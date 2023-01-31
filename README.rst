===============
 Ragweed Tests
===============

This is a set of test that verify functionality for the RGW, and the way it is represented in rados.

This can be used to verify functionality between upgrades.

Tests are run in two phases. In the first phase, (possibly when running against the old version) data is prepared.

In the second phase the representation of that data is then tested (possibly after an upgrade). 

Each of these phases can be executed separately.

For more information on the background of the tests visit: https://www.spinics.net/lists/ceph-devel/msg34636.html

The tests use the pytest test framework. To get started, ensure you have
the ``virtualenv`` software installed; e.g. on Debian/Ubuntu::

	sudo apt-get install python-virtualenv

on Fedora/RHEL::

	sudo yum install python3-virtualenv

and then run::

	./bootstrap

You will need to create a configuration file with the location of the
service and two different credentials. A sample configuration file named
``ragweed-example.conf`` has been provided in this repo. 

Once you have that file copied and edited, you can run the tests with::

	RAGWEED_CONF=ragweed.conf RAGWEED_STAGES=prepare,check ./virtualenv/bin/pytest -v

The phase(s) of the tests are set via ``RAGWEED_STAGES``. The options for ``RAGWEED_STAGES``  are ``prepare`` and ``check``. ``test`` can be used instead of ``check``.

=====================================
Running Ragweed Tests with vstart.sh
=====================================

Note: This example assumes the path to the ceph source code is $HOME/ceph.

The ``ragweed-example.conf`` file provided can be can be used to run the ragweed tests on a Ceph cluster started with vstart.

Before the ragweed tests are run a system user must be created on the cluster first. From the ``ceph/build`` directory run::

         $HOME/ceph/build/bin/radosgw-admin -c ceph.conf user create --uid=admin_user --display-name="Admin User" --access-key=accesskey2 --secret-key=secretkey2 --admin

If the system user created is different than the one created above the ``[user system]`` section in the ragweed.conf file much match the created user.

Then run ``$HOME/ceph/build/vstart_environment.sh`` or export the ``LD_LIBRARY_PATH`` and ``PYTHONPATH`` generated in the file ``$HOME/ceph/build/vstart_environment.sh``::

        chmod 775 $HOME/ceph/build/vstart_environment.sh
        $HOME/ceph/build/vstart_environment.sh

OR::

        export PYTHONPATH=$HOME/ceph/master/src/pybind:$HOME/ceph/master/build/lib/cython_modules/lib.3:$HOME/ceph/master/src/python-common:$PYTHONPATH
        export LD_LIBRARY_PATH=$HOME/ceph/master/build/lib:$LD_LIBRARY_PATH

Finally run the ragweed tests::

	RAGWEED_CONF=ragweed.conf RAGWEED_STAGES=prepare,check ./virtualenv/bin/pytest -v
