import sys
import os
import boto3
import botocore.exceptions
import json
import inspect
import pickle
import munch
import yaml
import configparser
import rados

from .reqs import _make_admin_request

import pytest

ragweed_env = None
suite = None

# Boto3 MultipartUpload compatibility class
class MultipartUpload:
    def __init__(self, connection, bucket_name, key_name, upload_id):
        self.connection = connection
        self.bucket_name = bucket_name
        self.key_name = key_name
        self.id = upload_id

    def upload_part_from_file(self, file_obj, part_num, headers=None):
        """Upload a part from file object (boto2 compatibility)"""
        kwargs = {
            'Bucket': self.bucket_name,
            'Key': self.key_name,
            'PartNumber': part_num,
            'UploadId': self.id,
            'Body': file_obj.read()
        }
        if headers:
            if 'Content-MD5' in headers:
                kwargs['ContentMD5'] = headers['Content-MD5']

        response = self.connection.client.upload_part(**kwargs)

        # Return an object with boto2-like attributes
        part = type('Part', (), {})()
        part.part_number = part_num
        part.etag = response['ETag']
        return part

    def complete_upload(self):
        """Complete the multipart upload (boto2 compatibility)"""
        # Get list of parts
        response = self.connection.client.list_parts(
            Bucket=self.bucket_name,
            Key=self.key_name,
            UploadId=self.id
        )

        parts = [{'ETag': part['ETag'], 'PartNumber': part['PartNumber']}
                for part in response['Parts']]

        self.connection.client.complete_multipart_upload(
            Bucket=self.bucket_name,
            Key=self.key_name,
            UploadId=self.id,
            MultipartUpload={'Parts': parts}
        )

# Boto3 Key-like wrapper for backward compatibility
class Key:
    def __init__(self, bucket):
        self.bucket = bucket
        self.key = None
        self.version_id = None
        self._metadata = None
        self._storage_class = None

    def _load_metadata(self):
        """Load object metadata if not already loaded"""
        if self._metadata is None and self.key:
            try:
                if hasattr(self.bucket, 'connection') and self.bucket.connection:
                    # BucketWrapper case
                    response = self.bucket.connection.client.head_object(
                        Bucket=self.bucket.name,
                        Key=self.key
                    )
                elif hasattr(self.bucket, 'Object'):
                    # Direct boto3 bucket case
                    obj = self.bucket.Object(self.key)
                    response = obj.head()
                else:
                    # BucketWrapper case without connection (after deserialization)
                    print(f"Warning: Cannot load metadata for {self.key} - bucket not properly initialized")
                    self._metadata = {}
                    return
                self._metadata = response
            except Exception as e:
                print(f"Warning: Failed to load metadata for {self.key}: {e}")
                self._metadata = {}

    @property
    def size(self):
        """Get object size (boto2 compatibility)"""
        self._load_metadata()
        return self._metadata.get('ContentLength', 0)

    @property
    def etag(self):
        """Get object ETag (boto2 compatibility)"""
        self._load_metadata()
        return self._metadata.get('ETag', '').strip('"')

    @property
    def name(self):
        """Get object name (boto2 compatibility)"""
        return self.key

    @property
    def storage_class(self):
        """Get object storage class (boto2 compatibility)"""
        # Return the explicitly set storage class if available
        if self._storage_class is not None:
            return self._storage_class
        # Otherwise load from metadata
        self._load_metadata()
        return self._metadata.get('StorageClass', 'STANDARD')

    @storage_class.setter
    def storage_class(self, value):
        """Set object storage class (boto2 compatibility)"""
        self._storage_class = value

    def set_contents_from_string(self, data, headers=None):
        kwargs = {'Key': self.key, 'Body': data}
        if headers:
            kwargs.update(headers)
        # Add storage class if set
        if self._storage_class:
            kwargs['StorageClass'] = self._storage_class
        self.bucket.put_object(**kwargs)
        # Clear metadata cache since object was modified
        self._metadata = None

    def get_contents_as_string(self):
        if hasattr(self.bucket, 'Object'):
            # Direct boto3 bucket case
            obj = self.bucket.Object(self.key)
            return obj.get()['Body'].read()
        else:
            # BucketWrapper case
            response = self.bucket.connection.client.get_object(
                Bucket=self.bucket.name,
                Key=self.key
            )
            return response['Body'].read()

class RGWConnection:
    def __init__(self, access_key, secret_key, host, port, is_secure):
        self.host = host
        self.port = port
        self.is_secure = is_secure

        # Construct endpoint URL for boto3
        protocol = 'https' if is_secure else 'http'
        endpoint_url = f"{protocol}://{host}:{port}" if port else f"{protocol}://{host}"

        # Create boto3 client and resource
        self.client = boto3.client(
            's3',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            endpoint_url=endpoint_url,
            use_ssl=is_secure
        )

        self.resource = boto3.resource(
            's3',
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            endpoint_url=endpoint_url,
            use_ssl=is_secure
        )

        # For backward compatibility, provide conn attribute
        self.conn = self

    def create_bucket(self, name):
        bucket = self.resource.Bucket(name)
        bucket.create()
        return BucketWrapper(bucket, self)

    def get_bucket(self, name, validate=True):
        bucket = self.resource.Bucket(name)
        if validate:
            # Check if bucket exists by trying to get its location
            try:
                self.client.head_bucket(Bucket=name)
            except botocore.exceptions.ClientError as e:
                error_code = e.response['Error']['Code']
                if error_code == '404':
                    raise botocore.exceptions.ClientError(
                        {'Error': {'Code': 'NoSuchBucket', 'Message': f'The specified bucket does not exist: {name}'}},
                        'HeadBucket'
                    )
                raise
        return BucketWrapper(bucket, self)


class BucketWrapper:
    """Wrapper to provide boto2-like interface for boto3 bucket"""
    def __init__(self, bucket, connection):
        self.bucket = bucket
        self.connection = connection
        self.name = bucket.name

    def __getattr__(self, name):
        # Delegate to underlying bucket for attributes we don't handle
        return getattr(self.bucket, name)

    def __getstate__(self):
        # For pickling - exclude non-serializable boto3 objects
        state = self.__dict__.copy()
        # Store just the bucket name for reconstruction
        state['_bucket_name'] = self.name
        # Remove unpicklable boto3 objects
        state.pop('bucket', None)
        state.pop('connection', None)
        return state

    def __setstate__(self, state):
        # For unpickling - will need to be reconstructed by framework
        self.__dict__.update(state)
        self.bucket = None
        self.connection = None

    def get_key(self, key_name, validate=False):
        """Get a key object (boto2 compatibility)"""
        if validate:
            try:
                self.connection.client.head_object(Bucket=self.name, Key=key_name)
            except botocore.exceptions.ClientError as e:
                if e.response['Error']['Code'] == '404':
                    return None
                raise

        # Return a Key-like object
        key = Key(self)
        key.key = key_name
        return key

    def list_multipart_uploads(self):
        """List multipart uploads (boto2 compatibility)"""
        response = self.connection.client.list_multipart_uploads(Bucket=self.name)
        uploads = []
        for upload in response.get('Uploads', []):
            # Create a full MultipartUpload object with all methods
            upload_obj = MultipartUpload(self.connection, self.name, upload['Key'], upload['UploadId'])
            upload_obj.initiated = upload['Initiated']
            uploads.append(upload_obj)
        return uploads

    def initiate_multipart_upload(self, key_name, headers=None):
        """Initiate multipart upload (boto2 compatibility)"""
        kwargs = {'Bucket': self.name, 'Key': key_name}
        if headers:
            # Map common headers
            if 'Content-Type' in headers:
                kwargs['ContentType'] = headers['Content-Type']
            # Check for storage class header (case-insensitive)
            if 'x-amz-storage-class' in headers:
                kwargs['StorageClass'] = headers['x-amz-storage-class']
            elif 'X-Amz-Storage-Class' in headers:
                kwargs['StorageClass'] = headers['X-Amz-Storage-Class']

        response = self.connection.client.create_multipart_upload(**kwargs)

        # Return a MultipartUpload object
        mp = MultipartUpload(self.connection, self.name, key_name, response['UploadId'])
        return mp

    def configure_versioning(self, versioning):
        """Configure bucket versioning (boto2 compatibility)"""
        status = 'Enabled' if versioning == 'true' or versioning is True else 'Suspended'
        self.connection.client.put_bucket_versioning(
            Bucket=self.name,
            VersioningConfiguration={'Status': status}
        )

    def copy_key(self, new_key_name, src_bucket_name, src_key_name, storage_class=None):
        """Copy key (boto2 compatibility)"""
        copy_source = {'Bucket': src_bucket_name, 'Key': src_key_name}
        kwargs = {'CopySource': copy_source, 'Bucket': self.name, 'Key': new_key_name}
        if storage_class:
            kwargs['StorageClass'] = storage_class
        self.connection.client.copy_object(**kwargs)


class RGWRESTAdmin:
    def __init__(self, connection):
        self.conn = connection

    def get_resource(self, path, params):
        r = _make_admin_request(self.conn, "GET", path, params)
        if r.status != 200:
            raise botocore.exceptions.ClientError(
                {'Error': {'Code': str(r.status), 'Message': r.reason}},
                'AdminRequest'
            )
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
        key_name = 'tests/' + test._name
        self.config_bucket.put_object(Key=key_name, Body=test.to_json())

    def read_test_data(self, test):
        key_name = 'tests/' + test._name
        obj = self.config_bucket.Object(key_name)
        s = obj.get()['Body'].read().decode('utf-8')
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

    def __getstate__(self):
        # For pickling - exclude non-serializable objects
        state = self.__dict__.copy()
        # Store bucket name instead of bucket object
        state['_bucket_name'] = self.name
        # Remove unpicklable objects
        state.pop('zone', None)
        state.pop('bucket', None)
        state.pop('placement_target', None)
        return state

    def __setstate__(self, state):
        # For unpickling - will need to be reconstructed by framework
        self.__dict__.update(state)
        self.zone = None
        self.bucket = None
        self.placement_target = None

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
        if not storage_class:
            storage_class = 'STANDARD'
        return self.storage_classes[storage_class] # may raise KeyError

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
        return self.get_placement_target(self.zone_params.default_placement)

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
        # Handle case where RBucket was serialized/deserialized and needs reconstruction
        if rbucket.bucket is None and hasattr(rbucket, '_bucket_name'):
            # Reconstruct from serialized state
            rbucket.zone = self
            rbucket.bucket = self.get_raw_bucket(rbucket._bucket_name)
            rbucket.bucket_info = self.rgw_rest_admin.get_bucket_instance_info(rbucket._bucket_name)
            rbucket.name = rbucket._bucket_name
            # Reconstruct placement info
            try:
                rbucket.placement_rule = RPlacementRule(rbucket.bucket_info.placement_rule)
                rbucket.placement_target = self.get_placement_target(rbucket.bucket_info.placement_rule)
            except:
                pass
        elif hasattr(rbucket.bucket, 'name'):
            rbucket.bucket = self.get_raw_bucket(rbucket.bucket.name)
            rbucket.bucket_info = self.rgw_rest_admin.get_bucket_instance_info(rbucket.bucket.name)
        else:
            # Old format compatibility
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
            cfg.read_file(f)

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
