from io import StringIO
import ragweed.framework
import binascii
import string
import random


from ragweed.framework import *

class obj_placement:
    def __init__(self, pool, oid, loc):
        self.pool = pool
        self.oid = oid
        self.loc = loc

def get_zone():
    return ragweed.framework.ragweed_env.zone

def rgwa():
    return get_zone().rgw_rest_admin

def get_pool_ioctx(pool_name):
    return ragweed.framework.ragweed_env.rados.open_ioctx(pool_name)


def get_placement(obj_json):
    try:
        return obj_placement(obj_json.pool, obj_json.oid, obj_json.loc)
    except:
        oid = obj_json.bucket.marker + '_' + obj_json.object
        key = ''
        if obj_json.key != '':
            key = obj_json.bucket.marker + '_' + obj_json.key
        return obj_placement(obj_json.bucket.pool, oid, key)

def validate_obj_location(rbucket, obj):
    expected_head_pool = rbucket.get_data_pool()
    head_pool_ioctx = get_pool_ioctx(expected_head_pool)
    print('expected head pool: ' + expected_head_pool)

    obj_layout = rgwa().get_obj_layout(obj)

    print('layout', obj_layout)

    obj_size = obj_layout.manifest.obj_size
    print('obj_size', obj_size)

    print('head', obj_layout.head)
    expected_tail_pool = rbucket.get_tail_pool(obj_layout)
    tail_pool_ioctx = get_pool_ioctx(expected_tail_pool)

    head_placement = get_placement(obj_layout.head)

    assert head_placement.pool == expected_head_pool

    # check rados object for head exists
    head_pool_ioctx.set_locator_key(head_placement.loc)
    (size, mtime) = head_pool_ioctx.stat(head_placement.oid)

    print('head size:', size, 'mtime:', mtime)

    # check tail
    for o in obj_layout.data_location:
        print('o=', o)
        print('ofs=', o.ofs, 'loc', o.loc)
        placement = get_placement(o.loc)
        if o.ofs < obj_layout.manifest.head_size:
            assert placement.pool == expected_head_pool
            pool_ioctx = head_pool_ioctx
        else:
            assert placement.pool == expected_tail_pool
            pool_ioctx = tail_pool_ioctx

        # validate rados object exists
        pool_ioctx.set_locator_key(placement.loc)
        (size, mtime) = pool_ioctx.stat(placement.oid)

        if o.ofs + o.loc_size > obj_size:
            # radosgw get_obj_layout() request on previous versions could return bigger o.loc_size
            # than actual at the end of the object. Fixed in later versions but need to adjust here
            # for when running on older versions.
            o.loc_size = obj_size - o.ofs

        check_size = o.loc_ofs + o.loc_size

        assert size == check_size

def calc_crc(data):
    crc = binascii.crc32(data)
    return '{:#010x}'.format(crc)


def validate_obj(rbucket, obj_name, expected_crc):
    b = rbucket.bucket

    obj = b.get_key(obj_name)

    validate_obj_location(rbucket, obj)
    obj_crc = calc_crc(obj.get_contents_as_string())
    assert obj_crc == expected_crc


def generate_random(size):
    """
    Generate random data
    (actually each MB is a repetition of the first KB)
    """
    chunk = 1024
    allowed = string.ascii_letters
    s = ''
    strpart = ''.join([allowed[random.randint(0, len(allowed) - 1)] for _ in range(chunk)])

    for y in range((size + chunk - 1) // chunk):
        this_chunk_len = chunk

        if len(s) + this_chunk_len > size:
            this_chunk_len = size - len(s)
            strpart = strpart[0:this_chunk_len]

        s += strpart

    return s

def gen_rand_string(size, chars=string.ascii_uppercase + string.digits):
    return ''.join(random.choice(chars) for _ in range(size))


# prepare:
# create objects in multiple sizes, with various names
# check:
# verify data correctness
# verify that objects were written to the expected data pool
class r_test_small_obj_data(RTest):
    def prepare(self):
        self.r_obj_names = [ 'obj', '_', '__', '_ _' ]
        self.r_bucket_sizes = {}

        sizes = { 0, 512 * 1024, 1024 * 1024 }

        for size in sizes:
            rbucket = self.create_bucket()
            self.r_bucket_sizes[rbucket.name] = size
            data = '0' * size
            for n in self.r_obj_names:
                obj = Key(rbucket.bucket)
                obj.key = n;
                obj.set_contents_from_string(data)

    def check(self):
        print(self.r_obj_names)
        for rbucket in self.get_buckets():
            size = self.r_bucket_sizes[rbucket.name]
            data = '0' * int(size)

            for n in self.r_obj_names:
                obj = Key(rbucket.bucket)
                obj.key = n;
                obj_data = obj.get_contents_as_string()
                assert bytearray(data, 'utf-8') == obj_data

                validate_obj_location(rbucket, obj)

class MultipartUploaderState:
    def __init__(self, mu):
        self.upload_id = mu.mp.id
        self.crc = mu.crc
        self.cur_part = mu.cur_part


class MultipartUploader:
    def __init__(self, rbucket, obj_name, size, part_size, storage_class=None, state=None):
        self.rbucket = rbucket
        self.obj_name = obj_name
        self.size = size
        self.part_size = part_size
        self.storage_class = storage_class
        self.crc = 0
        self.cur_part = 0

        if state is not None:
            self.crc = state.crc
            self.cur_part = state.cur_part

            for upload in rbucket.bucket.list_multipart_uploads():
                if upload.key_name == self.obj_name and upload.id == state.upload_id:
                    self.mp = upload

        self.num_full_parts = self.size // self.part_size
        self.last_part_size = self.size % self.part_size


    def prepare(self):
        headers = None
        if self.storage_class is not None:
            if not headers:
                headers = {}
            headers['X-Amz-Storage-Class'] = self.storage_class

        self.mp = self.rbucket.bucket.initiate_multipart_upload(self.obj_name, headers = headers)
        self.crc = 0
        self.cur_part = 0

    def upload(self):
        if self.cur_part > self.num_full_parts:
            return False

        if self.cur_part < self.num_full_parts:
            payload=gen_rand_string(self.part_size // (1024 * 1024)) * 1024 * 1024

            self.mp.upload_part_from_file(StringIO(payload), self.cur_part + 1)
            self.crc = binascii.crc32(bytearray(payload, 'utf-8'), self.crc)
            self.cur_part += 1

            return True


        if self.last_part_size > 0:
            last_payload='1'*self.last_part_size

            self.mp.upload_part_from_file(StringIO(last_payload), self.num_full_parts + 1)
            self.crc = binascii.crc32(bytearray(last_payload, 'utf-8'), self.crc)
            self.cur_part += 1

        return False

    def upload_all(self):
        while self.upload():
            pass

    def complete(self):
        self.mp.complete_upload()

    def hexdigest(self):
        return '{:#010x}'.format(self.crc)

    def get_state(self):
        return MultipartUploaderState(self)



# prepare:
# init, upload, and complete a multipart object
# check:
# verify data correctness
# verify that object layout is correct
class r_test_multipart_simple(RTest):
    def init(self):
        self.obj_size = 18 * 1024 * 1024
        self.part_size = 5 * 1024 * 1024

    def prepare(self):
        rb = self.create_bucket()
        self.r_obj = 'foo'

        uploader = MultipartUploader(rb, self.r_obj, self.obj_size, self.part_size)

        uploader.prepare()
        uploader.upload_all()
        uploader.complete()

        self.r_crc = uploader.hexdigest()
        print('written crc: ' + self.r_crc)

    def check(self):
        for rb in self.get_buckets():
            break

        k = rb.bucket.get_key(self.r_obj)
        assert k.size == self.obj_size

        validate_obj(rb, self.r_obj, self.r_crc)

# prepare:
# init, upload, and complete a multipart object
# check:
# part index is removed
class r_test_multipart_index_versioning(RTest):
    def init(self):
        self.obj_size = 18 * 1024 * 1024
        self.part_size = 5 * 1024 * 1024

    def prepare(self):
        rb = self.create_bucket()
        rb.bucket.configure_versioning('true')
        self.r_obj = 'foo'

        uploader = MultipartUploader(rb, self.r_obj, self.obj_size, self.part_size)

        uploader.prepare()
        uploader.upload_all()
        uploader.complete()

    def check(self):
        for rb in self.get_buckets():
            break
        index_check_result = rgwa().check_bucket_index(rb.name)
        print(index_check_result)

        assert len(index_check_result) == 0


# prepare:
# init, upload multipart object
# check:
# complete multipart
# verify data correctness
# verify that object layout is correct
class r_test_multipart_defer_complete(RTest):
    def init(self):
        self.obj_size = 18 * 1024 * 1024
        self.part_size = 5 * 1024 * 1024

    def prepare(self):
        rb = self.create_bucket()
        self.r_obj = 'foo'

        uploader = MultipartUploader(rb, self.r_obj, self.obj_size, self.part_size)

        uploader.prepare()
        uploader.upload_all()

        self.r_upload_state = uploader.get_state()


    def check(self):
        for rb in self.get_buckets():
            break

        uploader = MultipartUploader(rb, self.r_obj, self.obj_size, self.part_size,
                                     state=self.r_upload_state)

        uploader.complete()
        crc = uploader.hexdigest()
        print('written crc: ' + crc)

        k = rb.bucket.get_key(self.r_obj)
        assert k.size == self.obj_size

        validate_obj(rb, self.r_obj, crc)


# prepare:
# init, upload multipart object
# check:
# complete multipart
# verify data correctness
# verify that object layout is correct
class r_test_multipart_defer_update_complete(RTest):
    def init(self):
        self.obj_size = 18 * 1024 * 1024
        self.part_size = 5 * 1024 * 1024

    def prepare(self):
        rb = self.create_bucket()
        self.r_obj = 'foo'

        uploader = MultipartUploader(rb, self.r_obj, self.obj_size, self.part_size)

        uploader.prepare()
        ret = uploader.upload() # only upload one part
        assert ret == True

        self.r_upload_state = uploader.get_state()


    def check(self):
        for rb in self.get_buckets():
            break

        uploader = MultipartUploader(rb, self.r_obj, self.obj_size, self.part_size,
                                     state=self.r_upload_state)

        uploader.upload_all() # upload remaining
        uploader.complete()
        crc = uploader.hexdigest()
        print('written crc: ' + crc)

        k = rb.bucket.get_key(self.r_obj)
        assert k.size == self.obj_size

        validate_obj(rb, self.r_obj, crc)

class ObjInfo(object):
    pass

# prepare:
# init, create obj in different storage classes
# check:
# verify rados objects data, and in expected location
class r_test_obj_storage_class(RTest):
    def init(self):
        self.obj_size = 10 * 1024 * 1024

    def prepare(self):
        rb = self.create_bucket()
        zone = get_zone()

        placement_target = zone.get_placement_target(rb.placement_rule.placement_id)

        data = generate_random(self.obj_size)
        crc = calc_crc(bytearray(data, 'utf-8'))

        self.r_objs = []

        for sc in placement_target.storage_classes.get_all():
            obj_info = ObjInfo()

            obj_info.storage_class = sc
            obj_info.name = 'foo-' + sc
            obj_info.obj_size = self.obj_size
            obj_info.crc = crc

            obj = Key(rb.bucket)
            obj.key = obj_info.name
            obj.storage_class = sc
            obj.set_contents_from_string(data)

            self.r_objs.append(obj_info)


    def check(self):
        for rb in self.get_buckets():
            break

        for obj_info in self.r_objs:
            k = rb.bucket.get_key(obj_info.name)
            assert k.size == obj_info.obj_size
            assert k.storage_class == obj_info.storage_class


            validate_obj(rb, obj_info.name, obj_info.crc)

# prepare:
# init, start multipart obj creation in different storage classes
# check:
# complete uploads, verify rados objects data, and in expected location
class r_test_obj_storage_class_multipart(RTest):
    def init(self):
        self.obj_size = 11 * 1024 * 1024
        self.part_size = 5 * 1024 * 1024

    def prepare(self):
        rb = self.create_bucket()

        self.r_objs = []

        zone = get_zone()
        placement_target = zone.get_placement_target(rb.placement_rule.placement_id)

        for sc in placement_target.storage_classes.get_all():
            obj_info = ObjInfo()

            obj_info.storage_class = sc
            obj_info.name = 'foo-' + sc
            obj_info.obj_size = self.obj_size
            obj_info.part_size = self.part_size

            uploader = MultipartUploader(rb, obj_info.name, self.obj_size, self.part_size, storage_class=sc)

            uploader.prepare()
            uploader.upload_all()

            obj_info.upload_state = uploader.get_state()

            self.r_objs.append(obj_info)


    def check(self):
        for rb in self.get_buckets():
            break

        for obj_info in self.r_objs:
            uploader = MultipartUploader(rb, obj_info.name, obj_info.obj_size,
                                         obj_info.part_size, state=obj_info.upload_state)

            uploader.complete()
            crc = uploader.hexdigest()
            print('written crc: ' + crc)

            k = rb.bucket.get_key(obj_info.name)
            assert k.size == self.obj_size
            assert k.storage_class == obj_info.storage_class

            validate_obj(rb, obj_info.name, crc)


# prepare:
# init, create obj in different storage classes, copy them
# check:
# verify rados objects data, and in expected location
class r_test_obj_storage_class_copy(RTest):
    def init(self):
        self.obj_size = 5 * 1024 * 1024

    def prepare(self):
        rb = self.create_bucket()
        zone = get_zone()

        placement_target = zone.get_placement_target(rb.placement_rule.placement_id)

        data = generate_random(self.obj_size)
        crc = calc_crc(bytearray(data, 'utf-8'))

        self.r_objs = []

        for sc in placement_target.storage_classes.get_all():
            obj_info = ObjInfo()
            obj_info.storage_class = sc
            obj_info.name = 'foo-' + sc
            obj_info.obj_size = self.obj_size
            obj_info.crc = crc

            obj = Key(rb.bucket)
            obj.key = obj_info.name
            obj.storage_class = sc
            obj.set_contents_from_string(data)
            self.r_objs.append(obj_info)

            for sc2 in placement_target.storage_classes.get_all():
                copy_obj_info = ObjInfo()
                copy_obj_info.storage_class = sc2
                copy_obj_info.name = obj_info.name + '-' + sc2
                copy_obj_info.obj_size = obj_info.obj_size
                copy_obj_info.crc = obj_info.crc

                rb.bucket.copy_key(copy_obj_info.name, rb.bucket.name, obj_info.name, storage_class=copy_obj_info.storage_class)

                self.r_objs.append(copy_obj_info)



    def check(self):
        for rb in self.get_buckets():
            break

        for obj_info in self.r_objs:
            k = rb.bucket.get_key(obj_info.name)
            assert k.size == obj_info.obj_size
            assert k.storage_class == obj_info.storage_class

            print('validate', obj_info.name)

            validate_obj(rb, obj_info.name, obj_info.crc)

# prepare:
# init, create multipart obj in different storage classes, copy them
# check:
# verify rados objects data, and in expected location
class r_test_obj_storage_class_multipart_copy(RTest):
    def init(self):
        self.obj_size = 11 * 1024 * 1024
        self.part_size = 5 * 1024 * 1024

    def prepare(self):
        rb = self.create_bucket()
        zone = get_zone()

        placement_target = zone.get_placement_target(rb.placement_rule.placement_id)

        self.r_objs = []

        for sc in placement_target.storage_classes.get_all():
            obj_info = ObjInfo()
            obj_info.storage_class = sc
            obj_info.name = 'foo-' + sc
            obj_info.obj_size = self.obj_size

            uploader = MultipartUploader(rb, obj_info.name, self.obj_size, self.part_size, storage_class=sc)

            uploader.prepare()
            uploader.upload_all()
            uploader.complete()

            obj_info.crc = uploader.hexdigest()

            self.r_objs.append(obj_info)

            for sc2 in placement_target.storage_classes.get_all():
                copy_obj_info = ObjInfo()
                copy_obj_info.storage_class = sc2
                copy_obj_info.name = obj_info.name + '-' + sc2
                copy_obj_info.obj_size = obj_info.obj_size
                copy_obj_info.crc = obj_info.crc

                rb.bucket.copy_key(copy_obj_info.name, rb.bucket.name, obj_info.name, storage_class=copy_obj_info.storage_class)

                self.r_objs.append(copy_obj_info)



    def check(self):
        for rb in self.get_buckets():
            break

        for obj_info in self.r_objs:
            k = rb.bucket.get_key(obj_info.name)
            assert k.size == obj_info.obj_size
            assert k.storage_class == obj_info.storage_class

            print('validate', obj_info.name)

            validate_obj(rb, obj_info.name, obj_info.crc)

