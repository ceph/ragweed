import botocore.exceptions
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urlparse, urlencode

def _make_admin_request(conn, method, path, query_dict=None, body=None, response_headers=None, request_headers=None, expires_in=100000, path_style=True, timeout=None):
    """
    issue a request for a specified method, on a specified path
    with a specified (optional) body (encrypted per the connection), and
    return the response (status, reason).
    """

    query = ''
    if query_dict is not None:
        query = urlencode(query_dict)

    # For boto3, we need to generate URLs differently
    # Split path to get bucket and key components
    path_parts = path.split('/', 2)
    if len(path_parts) >= 2:
        bucket_str = path_parts[1]
    else:
        bucket_str = ''
    
    if len(path_parts) >= 3:
        key_str = path_parts[2]
    else:
        key_str = ''

    # Generate presigned URL using boto3 client
    try:
        if key_str:
            # Object-level request
            url = conn.client.generate_presigned_url(
                'get_object',
                Params={'Bucket': bucket_str, 'Key': key_str},
                ExpiresIn=expires_in,
                HttpMethod=method
            )
        else:
            # Bucket-level request
            url = conn.client.generate_presigned_url(
                'list_objects_v2',
                Params={'Bucket': bucket_str},
                ExpiresIn=expires_in,
                HttpMethod=method
            )
    except Exception:
        # Fallback for admin API paths that don't map to standard S3 operations
        protocol = 'https' if conn.is_secure else 'http'
        base_url = f"{protocol}://{conn.host}:{conn.port}" if conn.port else f"{protocol}://{conn.host}"
        url = base_url + path
        if query:
            url += '?' + query
        # For admin requests, we still need to use the raw request method
        return _make_raw_request(host=conn.host, port=conn.port, method=method, path=path + ('?' + query if query else ''), body=body, request_headers=request_headers, secure=conn.is_secure, timeout=timeout)

    o = urlparse(url)
    req_path = o.path + '?' + o.query + ('&' + query if query else '')

    return _make_raw_request(host=conn.host, port=conn.port, method=method, path=req_path, body=body, request_headers=request_headers, secure=conn.is_secure, timeout=timeout)

def _make_request(method, bucket, key, body=None, authenticated=False, response_headers=None, request_headers=None, expires_in=100000, path_style=True, timeout=None):
    """
    issue a request for a specified method, on a specified <bucket,key>,
    with a specified (optional) body (encrypted per the connection), and
    return the response (status, reason).

    If key is None, then this will be treated as a bucket-level request.

    If the request or response headers are None, then default values will be
    provided by later methods.
    """
    if not path_style:
        conn = bucket.connection
        request_headers['Host'] = conn.calling_format.build_host(conn.server_name(), bucket.name)

    if authenticated:
        urlobj = None
        if key is not None:
            urlobj = key
        elif bucket is not None:
            urlobj = bucket
        else:
            raise RuntimeError('Unable to find bucket name')
        url = urlobj.generate_url(expires_in, method=method, response_headers=response_headers, headers=request_headers)
        o = urlparse(url)
        path = o.path + '?' + o.query
    else:
        bucketobj = None
        if key is not None:
            path = '/{obj}'.format(obj=key.name)
            bucketobj = key.bucket
        elif bucket is not None:
            path = '/'
            bucketobj = bucket
        else:
            raise RuntimeError('Unable to find bucket name')
        if path_style:
            path = '/{bucket}'.format(bucket=bucketobj.name) + path

    return _make_raw_request(host=s3.main.host, port=s3.main.port, method=method, path=path, body=body, request_headers=request_headers, secure=s3.main.is_secure, timeout=timeout)

def _make_bucket_request(method, bucket, body=None, authenticated=False, response_headers=None, request_headers=None, expires_in=100000, path_style=True, timeout=None):
    """
    issue a request for a specified method, on a specified <bucket>,
    with a specified (optional) body (encrypted per the connection), and
    return the response (status, reason)
    """
    return _make_request(method=method, bucket=bucket, key=None, body=body, authenticated=authenticated, response_headers=response_headers, request_headers=request_headers, expires_in=expires_in, path_style=path_style, timeout=timeout)

def _make_raw_request(host, port, method, path, body=None, request_headers=None, secure=False, timeout=None):
    """
    issue a request to a specific host & port, for a specified method, on a
    specified path with a specified (optional) body (encrypted per the
    connection), and return the response (status, reason).

    This allows construction of special cases not covered by the bucket/key to
    URL mapping of _make_request/_make_bucket_request.
    """
    if secure:
        class_ = HTTPSConnection
    else:
        class_ = HTTPConnection

    if request_headers is None:
        request_headers = {}

    c = class_(host, port, timeout=timeout)

    # TODO: We might have to modify this in future if we need to interact with
    # how http.client.request handles Accept-Encoding and Host.
    c.request(method, path, body=body, headers=request_headers)

    res = c.getresponse()
    #c.close()

    return res


