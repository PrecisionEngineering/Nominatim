import os
from pathlib import Path
import tempfile

import psycopg2
import psycopg2.extras

from steps.utils import run_script

class NominatimEnvironment:
    """ Collects all functions for the execution of Nominatim functions.
    """

    def __init__(self, config):
        self.build_dir = Path(config['BUILDDIR']).resolve()
        self.src_dir = (Path(__file__) / '..' / '..' / '..' / '..').resolve()
        self.db_host = config['DB_HOST']
        self.db_port = config['DB_PORT']
        self.db_user = config['DB_USER']
        self.db_pass = config['DB_PASS']
        self.template_db = config['TEMPLATE_DB']
        self.test_db = config['TEST_DB']
        self.api_test_db = config['API_TEST_DB']
        self.server_module_path = config['SERVER_MODULE_PATH']
        self.reuse_template = not config['REMOVE_TEMPLATE']
        self.keep_scenario_db = config['KEEP_TEST_DB']
        self.code_coverage_path = config['PHPCOV']
        self.code_coverage_id = 1
        self.test_env = None

        self.template_db_done = False
        self.website_dir = None

    def connect_database(self, dbname):
        """ Return a connection to the database with the given name.
            Uses configured host, user and port.
        """
        dbargs = {'database': dbname}
        if self.db_host:
            dbargs['host'] = self.db_host
        if self.db_port:
            dbargs['port'] = self.db_port
        if self.db_user:
            dbargs['user'] = self.db_user
        if self.db_pass:
            dbargs['password'] = self.db_pass
        conn = psycopg2.connect(**dbargs)
        return conn

    def next_code_coverage_file(self):
        """ Generate the next name for a coverage file.
        """
        fn = Path(self.code_coverage_path) / "{:06d}.cov".format(self.code_coverage_id)
        self.code_coverage_id += 1

        return fn.resolve()

    def write_nominatim_config(self, dbname):
        """ Set up a custom test configuration that connects to the given
            database. This sets up the environment variables so that they can
            be picked up by dotenv and creates a project directory with the
            appropriate website scripts.
        """
        dsn = 'pgsql:dbname={}'.format(dbname)
        if self.db_host:
            dsn += ';host=' + self.db_host
        if self.db_port:
            dsn += ';port=' + self.db_port
        if self.db_user:
            dsn += ';user=' + self.db_user
        if self.db_pass:
            dsn += ';password=' + self.db_pass

        if self.website_dir is not None \
           and self.test_env is not None \
           and dsn == self.test_env['NOMINATIM_DATABASE_DSN']:
            return # environment already set uo

        self.test_env = os.environ
        self.test_env['NOMINATIM_DATABASE_DSN'] = dsn
        self.test_env['NOMINATIM_FLATNODE_FILE'] = ''
        self.test_env['NOMINATIM_IMPORT_STYLE'] = 'full'
        self.test_env['NOMINATIM_USE_US_TIGER_DATA'] = 'yes'

        if self.server_module_path:
            self.test_env['NOMINATIM_DATABASE_MODULE_PATH'] = self.server_module_path

        if self.website_dir is not None:
            self.website_dir.cleanup()

        self.website_dir = tempfile.TemporaryDirectory()
        self.run_setup_script('setup-website')


    def db_drop_database(self, name):
        """ Drop the database with the given name.
        """
        conn = self.connect_database('postgres')
        conn.set_isolation_level(0)
        cur = conn.cursor()
        cur.execute('DROP DATABASE IF EXISTS {}'.format(name))
        conn.close()

    def setup_template_db(self):
        """ Setup a template database that already contains common test data.
            Having a template database speeds up tests considerably but at
            the price that the tests sometimes run with stale data.
        """
        if self.template_db_done:
            return

        self.template_db_done = True

        if self.reuse_template:
            # check that the template is there
            conn = self.connect_database('postgres')
            cur = conn.cursor()
            cur.execute('select count(*) from pg_database where datname = %s',
                        (self.template_db,))
            if cur.fetchone()[0] == 1:
                return
            conn.close()
        else:
            # just in case... make sure a previous table has been dropped
            self.db_drop_database(self.template_db)

        try:
            # call the first part of database setup
            self.write_nominatim_config(self.template_db)
            self.run_setup_script('create-db', 'setup-db')
            # remove external data to speed up indexing for tests
            conn = self.connect_database(self.template_db)
            cur = conn.cursor()
            cur.execute("""select tablename from pg_tables
                           where tablename in ('gb_postcode', 'us_postcode')""")
            for t in cur:
                conn.cursor().execute('TRUNCATE TABLE {}'.format(t[0]))
            conn.commit()
            conn.close()

            # execute osm2pgsql import on an empty file to get the right tables
            with tempfile.NamedTemporaryFile(dir='/tmp', suffix='.xml') as fd:
                fd.write(b'<osm version="0.6"></osm>')
                fd.flush()
                self.run_setup_script('import-data',
                                      'ignore-errors',
                                      'create-functions',
                                      'create-tables',
                                      'create-partition-tables',
                                      'create-partition-functions',
                                      'load-data',
                                      'create-search-indices',
                                      osm_file=fd.name,
                                      osm2pgsql_cache='200')
        except:
            self.db_drop_database(self.template_db)
            raise


    def setup_api_db(self):
        """ Setup a test against the API test database.
        """
        self.write_nominatim_config(self.api_test_db)

    def setup_unknown_db(self):
        """ Setup a test against a non-existing database.
        """
        self.write_nominatim_config('UNKNOWN_DATABASE_NAME')

    def setup_db(self, context):
        """ Setup a test against a fresh, empty test database.
        """
        self.setup_template_db()
        self.write_nominatim_config(self.test_db)
        conn = self.connect_database(self.template_db)
        conn.set_isolation_level(0)
        cur = conn.cursor()
        cur.execute('DROP DATABASE IF EXISTS {}'.format(self.test_db))
        cur.execute('CREATE DATABASE {} TEMPLATE = {}'.format(self.test_db, self.template_db))
        conn.close()
        context.db = self.connect_database(self.test_db)
        psycopg2.extras.register_hstore(context.db, globally=False)

    def teardown_db(self, context):
        """ Remove the test database, if it exists.
        """
        if 'db' in context:
            context.db.close()

        if not self.keep_scenario_db:
            self.db_drop_database(self.test_db)

    def run_setup_script(self, *args, **kwargs):
        """ Run the Nominatim setup script with the given arguments.
        """
        self.run_nominatim_script('setup', *args, **kwargs)

    def run_update_script(self, *args, **kwargs):
        """ Run the Nominatim update script with the given arguments.
        """
        self.run_nominatim_script('update', *args, **kwargs)

    def run_nominatim_script(self, script, *args, **kwargs):
        """ Run one of the Nominatim utility scripts with the given arguments.
        """
        cmd = ['/usr/bin/env', 'php', '-Cq']
        cmd.append((Path(self.build_dir) / 'utils' / '{}.php'.format(script)).resolve())
        cmd.extend(['--' + x for x in args])
        for k, v in kwargs.items():
            cmd.extend(('--' + k.replace('_', '-'), str(v)))

        if self.website_dir is not None:
            cwd = self.website_dir.name
        else:
            cwd = self.build_dir

        run_script(cmd, cwd=cwd, env=self.test_env)
