#!/usr/bin/env python3

import bottle
from bottle import get, post, static_file, request, route, template
from bottle import SimpleTemplate
from configparser import ConfigParser
from ldap3 import Connection, Server
from ldap3 import SIMPLE, SUBTREE
from ldap3.core.exceptions import LDAPBindError, LDAPConstraintViolationResult, \
    LDAPInvalidCredentialsResult, LDAPUserNameIsMandatoryError, \
    LDAPSocketOpenError, LDAPExceptionError
import logging
import os
from os import environ, path


BASE_DIR = path.dirname(__file__)
LOG = logging.getLogger(__name__)
LOG_FORMAT = '%(asctime)s %(levelname)s: %(message)s'
VERSION = '2.1.0'


@get('/')
def get_index():
    return index_tpl()


@post('/')
def post_index():
    form = request.forms.getunicode

    def error(msg):
        return index_tpl(username=form('username'), alerts=[('error', msg)])

    if form('new-password') != form('confirm-password'):
        return error("Password doesn't match the confirmation!")

    if not password_is_strong(form('new-password')):
        return error("Password does not meet complexity/strength requirements!")

    try:
        change_passwords(form('username'), form('old-password'), form('new-password'))
    except Error as e:
        LOG.warning("Unsuccessful attempt to change password for %s: %s" % (form('username'), e))
        return error(str(e))

    LOG.info("Password successfully changed for: %s" % form('username'))

    return index_tpl(alerts=[('success', "Password has been changed")])


@route('/static/<filename>', name='static')
def serve_static(filename):
    return static_file(filename, root=path.join(BASE_DIR, 'static'))


def index_tpl(**kwargs):
    return template('index', **kwargs)


def connect_ldap(conf, **kwargs):
    server = Server(host=conf['host'],
                    port=conf.getint('port', None),
                    use_ssl=conf.getboolean('use_ssl', False),
                    connect_timeout=5)

    return Connection(server, raise_exceptions=True, **kwargs)


def change_passwords(username, old_pass, new_pass):
    changed = []

    for key in (key for key in CONF.sections()
                if key == 'ldap' or key.startswith('ldap:')):

        LOG.debug("Changing password in %s for %s" % (key, username))
        try:
            change_password(CONF[key], username, old_pass, new_pass)
            changed.append(key)
        except Error as e:
            for key in reversed(changed):
                LOG.info("Reverting password change in %s for %s" % (key, username))
                try:
                    change_password(CONF[key], username, new_pass, old_pass)
                except Error as e2:
                    LOG.error('{}: {!s}'.format(e.__class__.__name__, e2))
            raise e


def change_password(conf, *args):
    try:
        if conf.get('type') == 'ad':
            change_password_ad(conf, *args)
        else:
            change_password_ldap(conf, *args)

    except (LDAPBindError, LDAPInvalidCredentialsResult, LDAPUserNameIsMandatoryError):
        raise Error('Username or password is incorrect!')

    except LDAPConstraintViolationResult as e:
        # Extract useful part of the error message (for Samba 4 / AD).
        msg = e.message.split('check_password_restrictions: ')[-1].capitalize()
        raise Error(msg)

    except LDAPSocketOpenError as e:
        LOG.error('{}: {!s}'.format(e.__class__.__name__, e))
        raise Error('Unable to connect to the remote server.')

    except LDAPExceptionError as e:
        LOG.error('{}: {!s}'.format(e.__class__.__name__, e))
        raise Error('Encountered an unexpected error while communicating with the remote server.')


def change_password_ldap(conf, username, old_pass, new_pass):
    with connect_ldap(conf) as c:
        user_dn = find_user_dn(conf, c, username)

    # Note: raises LDAPUserNameIsMandatoryError when user_dn is None.
    with connect_ldap(conf, authentication=SIMPLE, user=user_dn, password=old_pass) as c:
        c.bind()
        c.extend.standard.modify_password(user_dn, old_pass, new_pass)


def change_password_ad(conf, username, old_pass, new_pass):
    user = username + '@' + conf['ad_domain']

    with connect_ldap(conf, authentication=SIMPLE, user=user, password=old_pass) as c:
        c.bind()
        user_dn = find_user_dn(conf, c, username)
        c.extend.microsoft.modify_password(user_dn, new_pass, old_pass)


def find_user_dn(conf, conn, uid):
    search_filter = conf['search_filter'].replace('{uid}', uid)
    conn.search(conf['base'], "(%s)" % search_filter, SUBTREE)

    return conn.response[0]['dn'] if conn.response else None


def read_config():
    config = ConfigParser()
    config.read([path.join(BASE_DIR, 'settings.ini'), os.getenv('CONF_FILE', '')])

    return config


# this returns True if password is strong (based on a set of checks)
def password_is_strong(password):
    if 'password_checker' in CONF.keys():
        conf = CONF['password_checker']
    else:
        return True
    # password length check
    if len(password) < conf.getint('min_length', 8):
        LOG.warning("Password is weak because it is too short.")
        return False
    # number of password complexity checks
    if conf.getboolean('mixed_case_required', False):
        if password.lower() == password or password.upper() == password:
            return False
    if conf.getboolean('digit_required', False):
        if not any([digit in password for digit in '0123456789']):
            return False
    if conf.getboolean('special_required', False):
        if not any([special in password for special in '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~']):
            return False
    # password dictionary check
    if conf.getboolean('dictionary_check_enabled', False):
        with open(conf['dictionary_file'], 'rt') as dictionary_file:
            for line in dictionary_file:
                if line.isspace():
                    continue
                pattern = line.rstrip().lower()
                if pattern in password.lower():
                    LOG.warning("Password is weak because its part is present in dictionary.")
                    return False
    # return True if not yet returned (password is strong)
    return True


class Error(Exception):
    pass


if environ.get('DEBUG'):
    bottle.debug(True)

# Set up logging.
logging.basicConfig(format=LOG_FORMAT)
LOG.setLevel(logging.INFO)
LOG.info("Starting ldap-passwd-webui %s" % VERSION)

CONF = read_config()

bottle.TEMPLATE_PATH = [BASE_DIR]

# Set default attributes to pass into templates.
SimpleTemplate.defaults = dict(CONF['html'])
SimpleTemplate.defaults['url'] = bottle.url


# Run bottle internal server when invoked directly (mainly for development).
if __name__ == '__main__':
    bottle.run(**CONF['server'])
# Run bottle in application mode (in production under uWSGI server).
else:
    application = bottle.default_app()
