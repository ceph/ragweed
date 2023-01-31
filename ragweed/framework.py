import sys
import os
import boto
import boto.s3.connection
import json
import inspect
import pickle
import munch
import yaml
import configparser
from boto.s3.key import Key
from nose.plugins.attrib import attr
from nose.tools import eq_ as eq
import rados

from .reqs import _make_admin_request

import pytest

ragweed_env = None
suite = None

class RGWConnection:
    def __init__(self, access_key, secret_key, host, port, is_secure):
        self.host = host
        self.port = port
        self.is_secure = is_secure
        self.conn = boto.connect_s3(
                aws_access_key_id = access_key,
                aws_secret_access_key = secret_key,
                host=host,
                port=port,
                is_secure=is_secure,
                calling_format = boto.s3.connection.OrdinaryCallingFormat(),
                )

    def create_bucket(self, name):
        return self.conn.create_bucket(name)

    def get_bucket(self, name, validate=True):
        return self.conn.get_bucket(name, validate=validate)


class RGWRESTAdmin:
    def __init__(self, connection):
        self.conn = connection

    def get_resource(self, path, params):
        r = _make_admin_request(self.conn, "GET", path, params)
        if r.status != 200:
            raise boto.exception.S3ResponseError(r.status, r.reason)
        return munch.munchify(json.loads(r.read()))


    def read_meta_key(self, key):
        return self.get_resource('/admin/metadata', {'key': key})

    def get_bucket_entrypoint(self, bucket_name):
        return self.read_meta_key('bucket:' + bucket_name)

    def get_bucket_instance_info(self, bucket_name, bucket_id = None):
        if not bucket_id:
            ep = self.get_bucket_entrypoint(bucket_name)
            print(ep)
            bucket_id = ep.data.bucket.bucket_id
        result = self.read_meta_key('bucket.instance:' + bucket_name + ":" + bucket_id)
        return result.data.bucket_info

    def check_bucket_index(self, bucket_name):
        return self.get_resource('/admin/bucket',{'index' : None, 'bucket':bucket_name})

    def get_obj_layout(self, key):
        path = '/' + key.bucket.name + '/' + key.name
        params = {'layout': None}
        if key.version_id is not None:
            params['versionId'] = key.version_id

        print(params)

        return self.get_resource(path, params)

    def get_zone_params(self):
        return self.get_resource('/admin/config', {'type': 'zone'})


class RSuite:
    def __init__(self, name, bucket_prefix, zone, suite_step):
        self.name = name
        self.bucket_prefix = bucket_prefix
        self.zone = zone
        self.config_bucket = None
        self.rtests = []
        self.do_preparing = False
        self.do_check = False
        for step in suite_step.split(','):
            if step == 'prepare':
                self.do_preparing = True
                self.config_bucket = self.zone.create_raw_bucket(self.get_bucket_name('conf'))
            if step == 'check' or step == 'test':
                self.do_check = True
                self.config_bucket = self.zone.get_raw_bucket(self.get_bucket_name('conf'))

    def get_bucket_name(self, suffix):
        return self.bucket_prefix + '-' + suffix

    def register_test(self, t):
        self.rtests.append(t)

    def write_test_data(self, test):
        k = Key(self.config_bucket)
        k.key = 'tests/' + test._name
        k.set_contents_from_string(test.to_json())

    def read_test_data(self, test):
        k = Key(self.config_bucket)
        k.key = 'tests/' + test._name
        s = k.get_contents_as_string()
        print('read_test_data=', s)
        test.from_json(s)

    def is_preparing(self):
        return self.do_preparing

    def is_checking(self):
        return self.do_check


class RTestJSONSerialize(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (list, dict, tuple, str, int, float, bool, type(None))):
            return JSONEncoder.default(self, obj)
        return {'__pickle': pickle.dumps(obj, 0).decode('utf-8')}

def rtest_decode_json(d):
    if '__pickle' in d:
        return pickle.loads(bytearray(d['__pickle'], 'utf-8'))
    return d

class RPlacementRule:
    def __init__(self, rule):
        r = rule.split('/', 1)

        self.placement_id = r[0]

        if (len(r) == 2):
            self.storage_class=r[1]
        else:
            self.storage_class = 'STANDARD'


class RBucket:
    def __init__(self, zone, bucket, bucket_info):
        self.zone = zone
        self.bucket = bucket
        self.name = bucket.name
        self.bucket_info = bucket_info

        try:
            self.placement_rule = RPlacementRule(self.bucket_info.placement_rule)
            self.placement_target = self.zone.get_placement_target(self.bucket_info.placement_rule)
        except:
            pass

    def get_data_pool(self):
        try:
            # old style explicit pool
            explicit_pool = self.bucket_info.bucket.pool
        except:
            # new style explicit pool
            explicit_pool = self.bucket_info.bucket.explicit_placement.data_pool
        if explicit_pool is not None and explicit_pool != '':
            return explicit_pool

        return self.placement_target.get_data_pool(self.placement_rule)


    def get_tail_pool(self, obj_layout):
        try:
            placement_rule = obj_layout.manifest.tail_placement.placement_rule
        except:
            placement_rule = ''
        if placement_rule == '':
                try:
                    # new style
                    return obj_layout.manifest.tail_placement.bucket.explicit_placement.data_pool
                except:
                    pass

                try:
                    # old style
                    return obj_layout.manifest.tail_bucket.pool
                except:
                    pass

        pr = RPlacementRule(placement_rule)

        return self.placement_target.get_data_pool(pr)

class RStorageClasses:
    def __init__(self, config):
        if hasattr(config, 'storage_classes'):
            self.storage_classes = config.storage_classes
        else:
            try:
                self.storage_classes = munch.munchify({ 'STANDARD': { 'data_pool': config.data_pool }})
            except:
                self.storage_classes = None
                pass

    def get(self, storage_class):
        assert(self.storage_classes != None)
        try:
            if not storage_class:
                storage_class = 'STANDARD'
            sc = self.storage_classes[storage_class]
        except:
            eq('could not find storage class ' + storage_class, 0)

        return sc

    def get_all(self):
        for (name, _) in self.storage_classes.items():
            yield name

class RPlacementTarget:
    def __init__(self, name, config):
        self.name = name
        self.index_pool = config.index_pool
        self.data_extra_pool = config.data_extra_pool
        self.storage_classes = RStorageClasses(config)

        if not self.data_extra_pool:
            self.data_extra_pool = self.storage_classes.get_data_pool('STANDARD')

    def get_data_pool(self, placement_rule):
        return self.storage_classes.get(placement_rule.storage_class).data_pool

class RZone:
    def __init__(self, conn):
        self.conn = conn

        self.rgw_rest_admin = RGWRESTAdmin(self.conn.system)
        self.zone_params = self.rgw_rest_admin.get_zone_params()

        self.placement_targets = {}

        for e in self.zone_params.placement_pools:
            self.placement_targets[e.key] = e.val

        print('zone_params:', self.zone_params)

    def get_placement_target(self, placement_id):
        plid = placement_id
        if placement_id is None or placement_id == '':
            print('zone_params=', self.zone_params)
            plid = self.zone_params.default_placement

        try:
            return RPlacementTarget(plid, self.placement_targets[plid])
        except:
            pass

        return None

    def get_default_placement(self):
        return get_placement_target(self.zone_params.default_placement)

    def create_bucket(self, name):
        bucket = self.create_raw_bucket(name)
        bucket_info = self.rgw_rest_admin.get_bucket_instance_info(bucket.name)
        print('bucket_info:', bucket_info)
        return RBucket(self, bucket, bucket_info)

    def get_bucket(self, name):
        bucket = self.get_raw_bucket(name)
        bucket_info = self.rgw_rest_admin.get_bucket_instance_info(bucket.name)
        print('bucket_info:', bucket_info)
        return RBucket(self, bucket, bucket_info)

    def create_raw_bucket(self, name):
        return self.conn.regular.create_bucket(name)

    def get_raw_bucket(self, name):
        return self.conn.regular.get_bucket(name)

    def refresh_rbucket(self, rbucket):
        rbucket.bucket = self.get_raw_bucket(rbucket.bucket.name)
        rbucket.bucket_info = self.rgw_rest_admin.get_bucket_instance_info(rbucket.bucket.name)


class RTest:
    def setup_method(self):
        self._name = self.__class__.__name__
        self.r_buckets = []
        self.init()

    def create_bucket(self):
        bid = len(self.r_buckets) + 1
        bucket_name =  suite.get_bucket_name(self._name + '.' + str(bid))
        bucket_name = bucket_name.replace("_", "-")
        rb = suite.zone.create_bucket(bucket_name)
        self.r_buckets.append(rb)

        return rb

    def get_buckets(self):
        for rb in self.r_buckets:
            yield rb

    def init(self):
        pass

    def prepare(self):
        pass

    def check(self):
        pass

    def to_json(self):
        attrs = {}
        for x in dir(self):
            if x.startswith('r_'):
                attrs[x] = getattr(self, x)
        return json.dumps(attrs, cls=RTestJSONSerialize)

    def from_json(self, s):
        j = json.loads(s, object_hook=rtest_decode_json)
        for e in j:
            setattr(self, e, j[e])

    def save(self):
        suite.write_test_data(self)

    def load(self):
        suite.read_test_data(self)
        for rb in self.r_buckets:
            suite.zone.refresh_rbucket(rb)

    def test(self):
        suite.register_test(self)
        if suite.is_preparing():
            self.prepare()
            self.save()

        if suite.is_checking():
            self.load()
            self.check()

def read_config(fp):
    config = munch.Munch()
    g = yaml.safe_load_all(fp)
    for new in g:
        print(munch.munchify(new))
        config.update(munch.munchify(new))
    return config

str_config_opts = [
                'user_id',
                'access_key',
                'secret_key',
                'host',
                'ceph_conf',
                'bucket_prefix',
                ]

int_config_opts = [
                'port',
                ]

bool_config_opts = [
                'is_secure',
                ]

def dict_find(d, k):
    if k in d:
        return d[k]
    return None

class RagweedEnv:
    def __init__(self):
        self.config = munch.Munch()

        cfg = configparser.RawConfigParser()
        try:
            path = os.environ['RAGWEED_CONF']
        except KeyError:
            raise RuntimeError(
                'To run tests, point environment '
                + 'variable RAGWEED_CONF to a config file.',
                )
        with open(path, 'r') as f:
            cfg.readfp(f)

        for section in cfg.sections():
            try:
                (section_type, name) = section.split(None, 1)
                if not section_type in self.config:
                    self.config[section_type] = munch.Munch()
                self.config[section_type][name] = munch.Munch()
                cur = self.config[section_type]
            except ValueError:
                section_type = ''
                name = section
                self.config[name] = munch.Munch()
                cur = self.config

            cur[name] = munch.Munch()

            for var in str_config_opts:
                try:
                    cur[name][var] = cfg.get(section, var)
                except configparser.NoOptionError:
                    pass

            for var in int_config_opts:
                try:
                    cur[name][var] = cfg.getint(section, var)
                except configparser.NoOptionError:
                    pass

            for var in bool_config_opts:
                try:
                    cur[name][var] = cfg.getboolean(section, var)
                except configparser.NoOptionError:
                    pass

        print(json.dumps(self.config))

        rgw_conf = self.config.rgw

        try:
            self.bucket_prefix = rgw_conf.bucket_prefix
        except:
            self.bucket_prefix = 'ragweed'

        conn = munch.Munch()
        for (k, u) in self.config.user.items():
            conn[k] = RGWConnection(u.access_key, u.secret_key, rgw_conf.host, dict_find(rgw_conf, 'port'), dict_find(rgw_conf, 'is_secure'))

        self.zone = RZone(conn)
        self.suite = RSuite('ragweed', self.bucket_prefix, self.zone, os.environ['RAGWEED_STAGES'])

        try:
            self.ceph_conf = self.config.rados.ceph_conf
        except:
            raise RuntimeError(
                'ceph_conf is missing under the [rados] section in ' + os.environ['RAGWEED_CONF']
                )

        print('conf=' + str(self.ceph_conf))
        self.rados = rados.Rados(conffile=self.ceph_conf)
        self.rados.connect()

        pools = self.rados.list_pools()

        for pool in pools:
             print("rados pool>", pool)

def setup_module():
    global ragweed_env
    global suite

    ragweed_env = RagweedEnv()
    suite = ragweed_env.suite

@pytest.fixture(scope="package", autouse=True)
def setup_teardown():
    setup_module()
    yield
