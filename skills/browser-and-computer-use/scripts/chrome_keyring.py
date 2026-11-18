#!/usr/bin/env python3
"""Refuse to start Chrome on the user's own profile while the keyring that holds its cookie key is locked. Linux only.

  python3 chrome_keyring.py check     exit 0 when the default keyring is unlocked; otherwise exit 1 and say how
                                      to unlock it
  uv run --with jeepney python -I chrome_keyring.py unlock
                                      for the user: ask for the keyring's password in this terminal and unlock it

Chrome on Linux encrypts cookies with a key it keeps in the desktop's default Secret Service keyring. Started while
that keyring is locked, it asks for an unlock, and when nobody answers it can't decrypt those cookies. Chrome 157
then deletes them, with every other cookie of the same site, as LoadCookiesForDomains in
net/extras/sqlite/sqlite_persistent_cookie_store.cc does, which signs the user out of every site. On WSL on
18 November 2026 an agent started the user's Chrome while the keyring was locked, the unlock prompt couldn't open,
and afterwards the profile's cookie file held only the 13 cookies made after that start.

`unlock` exists because on WSL the keyring's own prompt then failed with "cannot open display", so the user had no
other way to unlock it. It sends the password to gnome-keyring over the session bus, which only the user's own
processes reach, and never prints or stores it.
"""

import getpass
import os
import subprocess
import sys

SERVICE = 'org.freedesktop.secrets'
DEFAULT = '/org/freedesktop/secrets/aliases/default'
COMMAND = f'uv run --with jeepney python -I {os.path.abspath(__file__)} unlock'


def refusal(env=None):
    """'' when the default keyring is unlocked, else why Chrome must not start on the user's profile and the fix."""
    try:
        result = subprocess.run(['busctl', '--user', 'get-property', SERVICE, DEFAULT,
                                 'org.freedesktop.Secret.Collection', 'Locked'],
                                capture_output=True, text=True, timeout=30, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        state = f'could not ask the keyring whether it is locked ({error})'
    else:
        answer = result.stdout.strip()
        if answer == 'b false':
            return ''
        state = ("the default keyring, which holds Chrome's cookie key, is locked" if answer == 'b true' else
                 f'the keyring did not say whether it is locked ({(result.stderr or result.stdout).strip()[-200:]})')
    return (f"{state}. Chrome started on the user's own profile now would delete every cookie it can't decrypt and "
            'sign the user out of every site, so it was not started. Ask the user to unlock the keyring by running '
            f'`{COMMAND}` in a terminal on this machine and typing its password, then run this again. Never unlock it '
            'yourself.')


def unlock():
    from jeepney import DBusAddress, new_method_call
    from jeepney.io.blocking import open_dbus_connection
    from jeepney.wrappers import DBusErrorResponse, unwrap_msg

    service = DBusAddress('/org/freedesktop/secrets', SERVICE, 'org.freedesktop.Secret.Service')
    internal = DBusAddress('/org/freedesktop/secrets', SERVICE,
                           'org.gnome.keyring.InternalUnsupportedGuiltRiddenInterface')
    with open_dbus_connection(bus='SESSION') as bus:
        def call(address, method, signature, *args):
            return unwrap_msg(bus.send_and_get_reply(new_method_call(address, method, signature, args)))

        collection = call(service, 'ReadAlias', 's', 'default')[0]
        if collection == '/':
            return 'chrome_keyring: there is no default keyring to unlock'
        properties = DBusAddress(collection, SERVICE, 'org.freedesktop.DBus.Properties')
        if not call(properties, 'Get', 'ss', 'org.freedesktop.Secret.Collection', 'Locked')[0][1]:
            print('the default keyring is already unlocked')
            return 0
        label = call(properties, 'Get', 'ss', 'org.freedesktop.Secret.Collection', 'Label')[0][1]
        session = call(service, 'OpenSession', 'sv', 'plain', ('s', ''))[1]
        password = getpass.getpass(f'Password of the keyring "{label}": ').encode()
        try:
            call(internal, 'UnlockWithMasterPassword', 'o(oayays)', collection, (session, b'', password, 'text/plain'))
        except DBusErrorResponse as error:
            return f'chrome_keyring: the keyring stayed locked ({error.name}); check the password and run this again'
        if call(properties, 'Get', 'ss', 'org.freedesktop.Secret.Collection', 'Locked')[0][1]:
            return 'chrome_keyring: the keyring is still locked; was the password right?'
    print(f'unlocked the keyring "{label}"')
    return 0


def main(argv):
    if argv == ['check']:
        reason = refusal()
        if reason:
            print(f'chrome_keyring: {reason}', file=sys.stderr)
            return 1
        print('the default keyring is unlocked')
        return 0
    if argv == ['unlock']:
        return unlock()
    return __doc__


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
