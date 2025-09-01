#!/usr/bin/python3
from setuptools import setup, find_packages

setup(
    name='ragweed',
    version='0.0.1',
    packages=find_packages(),

    author='Yehuda Sadeh',
    author_email='yehuda@redhat.com',
    description='A test suite for ceph rgw',
    license='MIT',
    keywords='ceph rgw testing',

    install_requires=[
        'boto3',
        'PyYAML',
        'munch >=1.0.0',
        'gevent >=1.0',
        'isodate >=0.4.4',
        ],

    #entry_points={
    #    'console_scripts': [
    #        'ragweed = ragweed:main',
    #        ],
    #    },

    )
