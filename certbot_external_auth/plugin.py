#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Manual plugin on stereoids."""

from past.builtins import basestring
from builtins import bytes
import six

import atexit
import calendar
import collections
import json
import logging
import math
import os
import pipes
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import datetime

from collections import OrderedDict

from acme import challenges
from acme import errors as acme_errors

try:
    from acme.jose import b64
except:
    from josepy import b64

from certbot import errors
from certbot import interfaces
from certbot import reverter
from certbot.display import util as display_util
from certbot.plugins import common

from six.moves import queue  # pylint: disable=import-error

from certbot_external_auth import *

logger = logging.getLogger(__name__)


INITIAL_PID = os.getpid()


class AutoJSONEncoder(json.JSONEncoder):
    """
    JSON encoder trying to_json() first
    """
    def default(self, obj):
        try:
            return obj.to_json()
        except AttributeError:
            return self.default_classic(obj)

    def default_classic(self, o):
        if isinstance(o, set):
            return list(o)
        elif isinstance(o, datetime.datetime):
            return (o - datetime.datetime(1970, 1, 1)).total_seconds()
        elif isinstance(o, bytes):
            return o.decode('UTF-8')
        else:
            return super(AutoJSONEncoder, self).default(o)


class AuthenticatorOut(common.Plugin):
    """Manual Authenticator.

    This plugin requires user's manual intervention in setting up a HTTP
    server for solving http-01 challenges and thus does not need to be
    run as a privileged process. Alternatively shows instructions on how
    to use Python's built-in HTTP server.

    Script is also based on https://github.com/marcan/certbot-external

    """
    hidden = True

    description = "Manual challenge solver"

    MESSAGE_TEMPLATE = {
        "dns-01": """\
Please deploy a DNS TXT record under the name
{domain} with the following value:

{validation}

Once this is deployed,
""",
        "http-01": """\
Make sure your web server displays the following content at
{uri} before continuing:

{validation}

If you don't have HTTP server configured, you can run the following
command on the target server (as root):

{command}
"""
    }

    # a disclaimer about your current IP being transmitted to Let's Encrypt's servers.
    IP_DISCLAIMER = """\
NOTE: The IP of this machine will be publicly logged as having requested this certificate. \
If you're running certbot in manual mode on a machine that is not your server, \
please ensure you're okay with that.

Are you OK with your IP being logged?
"""

    # "cd /tmp/certbot" makes sure user doesn't serve /root,
    # separate "public_html" ensures that cert.pem/key.pem are not
    # served and makes it more obvious that Python command will serve
    # anything recursively under the cwd

    CMD_TEMPLATE = """\
mkdir -p {root}/public_html/{achall.URI_ROOT_PATH}
cd {root}/public_html
echo '{validation}' > {achall.URI_ROOT_PATH}/{encoded_token}
# run only once per server:
$(command -v python2 || command -v python2.7 || command -v python2.6) -c \\
"import BaseHTTPServer, SimpleHTTPServer; \\
s = BaseHTTPServer.HTTPServer(('0.0.0.0', {port}), SimpleHTTPServer.SimpleHTTPRequestHandler); \\
s.serve_forever()" """
    """Command template."""

    # Reporter stuff
    HIGH_PRIORITY = 0
    """High priority constant. See `add_message`."""
    MEDIUM_PRIORITY = 1
    """Medium priority constant. See `add_message`."""
    LOW_PRIORITY = 2
    """Low priority constant. See `add_message`."""

    _msg_type = collections.namedtuple('ReporterMsg', 'priority text on_crash')

    def __init__(self, *args, **kwargs):
        super(AuthenticatorOut, self).__init__(*args, **kwargs)
        self._root = (tempfile.mkdtemp() if self.conf("test-mode")
                      else "/tmp/certbot")
        self._httpd = None
        self._start_time = calendar.timegm(time.gmtime())
        self._handler_file_problem = False

        # Set up reverter
        self.reverter = reverter.Reverter(self.config)
        self.reverter.recovery_routine()

        # Reporter
        self.orig_reporter = None
        self.messages = queue.PriorityQueue()

    @classmethod
    def add_parser_arguments(cls, add):
        add("test-mode", action="store_true",
            help="Test mode. Executes the manual command in subprocess.")
        add("public-ip-logging-ok", action="store_true",
            help="Automatically allows public IP logging.")
        add("text-mode", action="store_true",
            help="Original text mode, by default turned off, produces JSON challenges")
        add("handler", default=None,
            help="Handler program that takes the action. Data is transferred in ENV vars")
        add("dehydrated-dns", action="store_true",
            help="Switches handler mode to Dehydrated DNS compatible version")

    def prepare(self):  # pylint: disable=missing-docstring,no-self-use
        # Re-register reporter - json only report
        atexit.register(self.atexit_print_messages)

        # Re-register displayer - stderr only displayer
        #displayer = display_util.NoninteractiveDisplay(sys.stderr)
        displayer = display_util.FileDisplay(sys.stderr, False)

        # Non-interactive not yet supported
        if self.config.noninteractive_mode and not self.conf("test-mode"):
            raise errors.PluginError("Running manual mode non-interactively is not supported (yet)")
        if not self._is_handler_mode() and self._is_dehydrated_dns():
            raise errors.PluginError("dehydrated-dns switch is allowed only with handler specified")

    def more_info(self):  # pylint: disable=missing-docstring,no-self-use
        return ("This plugin requires user's manual intervention in setting "
                "up challenges to prove control of a domain and does not need "
                "to be run as a privileged process. When solving "
                "http-01 challenges, the user is responsible for setting up "
                "an HTTP server. Alternatively, instructions are shown on how "
                "to use Python's built-in HTTP server. The user is "
                "responsible for configuration of a domain's DNS when solving "
                "dns-01 challenges. The type of challenges used can be "
                "controlled through the --preferred-challenges flag.")

    def get_chall_pref(self, domain):
        # pylint: disable=missing-docstring,no-self-use,unused-argument
        return [challenges.DNS01, challenges.HTTP01]

    def perform(self, achalls):
        """
        Performs the actual challenge resolving.
        :param achalls:
        :return:
        """
        # pylint: disable=missing-docstring
        self._get_ip_logging_permission()
        mapping = {"http-01": self._perform_http01_challenge,
                   "dns-01": self._perform_dns01_challenge
                   }
        responses = []
        # TODO: group achalls by the same socket.gethostbyname(_ex)
        # and prompt only once per server (one "echo -n" per domain)

        if self._is_classic_handler_mode() and self._call_handler("pre-perform") is None:
            raise errors.PluginError("Error in calling the handler to do the pre-perform (challenge) stage")

        for achall in achalls:
            responses.append(mapping[achall.typ](achall))

        if self._is_classic_handler_mode() and self._call_handler("post-perform") is None:
            raise errors.PluginError("Error in calling the handler to do the post-perform (challenge) stage")

        return responses

    def add_message(self, msg, priority, on_crash=True):
        """Adds msg to the list of messages to be printed.

        :param str msg: Message to be displayed to the user.

        :param int priority: One of HIGH_PRIORITY, MEDIUM_PRIORITY, or
            LOW_PRIORITY.

        :param bool on_crash: Whether or not the message should be printed if
            the program exits abnormally.

        """
        if self._is_text_mode():
            self.orig_reporter.add_message(msg, priority, on_crash=on_crash)
            return

        assert self.HIGH_PRIORITY <= priority <= self.LOW_PRIORITY
        self.messages.put(self._msg_type(priority, msg, on_crash))
        logger.debug("Reporting to user: %s", msg)
        pass

    def print_messages(self):
        """Prints messages to the user and clears the message queue."""
        if self._is_text_mode():
            self.orig_reporter.print_messages()
            return

        no_exception = sys.exc_info()[0] is None
        messages = []
        while not self.messages.empty():
            msg = self.messages.get()
            if self.config.quiet:
                # In --quiet mode, we only print high priority messages that
                # are flagged for crash cases
                if not (msg.priority == self.HIGH_PRIORITY and msg.on_crash):
                    continue
            if no_exception or msg.on_crash:
                cur_message = OrderedDict()
                cur_message['priority'] = msg.priority
                cur_message['on_crash'] = msg.on_crash
                cur_message['lines'] = msg.text.splitlines()
                messages.append(cur_message)

        data = OrderedDict()
        data[FIELD_CMD] = COMMAND_REPORT
        data['messages'] = messages
        self._json_out(data, True)
        pass

    def atexit_print_messages(self, pid=None):
        """Function to be registered with atexit to print messages.

        :param int pid: Process ID

        """
        if pid is None:
            pid = INITIAL_PID
        # This ensures that messages are only printed from the process that
        # created the Reporter.
        if pid == os.getpid():
            self.print_messages()

    @classmethod
    def _test_mode_busy_wait(cls, port):
        while True:
            time.sleep(1)
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.connect(("localhost", port))
            except socket.error:  # pragma: no cover
                pass
            else:
                break
            finally:
                sock.close()

    def cleanup(self, achalls):
        """
        Cleaning up challenges, called by AuthHandler
        :param achalls:
        :return:
        """
        # pylint: disable=missing-docstring

        if self._is_classic_handler_mode() \
                and not self._is_handler_broken() \
                and self._call_handler("pre-cleanup") is None:
            raise errors.PluginError("Error in calling the handler to do the pre-cleanup stage")

        for achall in achalls:
            cur_record = self._get_cleanup_json(achall)

            if self._is_json_mode() or self._is_handler_mode():
                self._json_out(cur_record, True)

            if self._is_handler_mode() \
                    and not self._is_handler_broken() \
                    and self._call_handler("cleanup", **(self._get_json_to_kwargs(cur_record))) is None:
                raise errors.PluginError("Error in calling the handler to do the cleanup stage")

            if isinstance(achall.chall, challenges.HTTP01):
                self._cleanup_http01_challenge(achall)

        if self._is_classic_handler_mode() \
                and not self._is_handler_broken() \
                and self._call_handler("post-cleanup") is None:
            raise errors.PluginError("Error in calling the handler to do the post-cleanup stage")

    def _get_cleanup_json(self, achall):
        response, validation = achall.response_and_validation()

        cur_record = OrderedDict()
        cur_record[FIELD_CMD] = COMMAND_CLEANUP
        cur_record[FIELD_TYPE] = achall.chall.typ

        if isinstance(achall.chall, challenges.HTTP01):
            pass
        elif isinstance(achall.chall, challenges.DNS01):
            pass

        cur_record[FIELD_STATUS] = None
        cur_record[FIELD_DOMAIN] = achall.domain
        cur_record[FIELD_TOKEN] = b64.b64encode(achall.chall.token)
        if type(cur_record[FIELD_TOKEN]) == bytes:
            cur_record[FIELD_TOKEN] = cur_record[FIELD_TOKEN].decode('UTF-8')
        cur_record[FIELD_VALIDATION] = validation if isinstance(validation, basestring) else ''
        cur_record[FIELD_KEY_AUTH] = response.key_authorization.decode('UTF-8') if isinstance(response.key_authorization, bytes) else response.key_authorization
        cur_record[FIELD_VALIDATED] = None
        cur_record[FIELD_ERROR] = None

        if achall.status is not None:
            try:
                cur_record[FIELD_STATUS] = achall.status.name
            except:
                pass

        if achall.error is not None:
            try:
                cur_record[FIELD_ERROR] = str(achall.error)
            except:
                cur_record[FIELD_ERROR] = 'ERROR'

        if achall.validated is not None:
            try:
                cur_record[FIELD_VALIDATED] = str(achall.validated)
            except:
                cur_record[FIELD_VALIDATED] = 'ERROR'

        return cur_record

    def _get_json_to_kwargs(self, json_data):
        """
        Augments json data before passing to the handler script.
        Prefixes all keys with cbot_ value to avoid clashes + serializes
        itself to JSON - for JSON parsing stuff.

        :param json_data:
        :return:
        """
        n_data = OrderedDict()
        for k in json_data:
            val = json_data[k]
            if k == 'command':
                continue
            if isinstance(val, float):
                val = str(math.ceil(val))
            if not isinstance(val, (str, basestring)):
                val = str(val)
            if val is not None:
                n_data[k] = val
                n_data['cbot_' + k] = val

        n_data['cbot_json'] = self._json_dumps(json_data)
        return n_data

    def _perform_http01_challenge(self, achall):
        # same path for each challenge response would be easier for
        # users, but will not work if multiple domains point at the
        # same server: default command doesn't support virtual hosts
        response, validation = achall.response_and_validation()
        port = (response.port if self.config.http01_port is None
                else int(self.config.http01_port))

        command = self.CMD_TEMPLATE.format(
            root=self._root, achall=achall, response=response,
            validation=pipes.quote(validation),
            encoded_token=achall.chall.encode("token"),
            port=port)

        json_data = OrderedDict()
        json_data[FIELD_CMD] = COMMAND_PERFORM
        json_data[FIELD_TYPE] = achall.chall.typ
        json_data[FIELD_DOMAIN] = achall.domain
        json_data[FIELD_TOKEN] = b64.b64encode(achall.chall.token)
        json_data[FIELD_VALIDATION] = validation
        json_data[FIELD_URI] = achall.chall.uri(achall.domain)
        json_data['command'] = command
        json_data[FIELD_KEY_AUTH] = response.key_authorization

        json_data = self._json_sanitize_dict(json_data)

        if self.conf("test-mode"):
            logger.debug("Test mode. Executing the manual command: %s", command)
            # sh shipped with OS X does't support echo -n, but supports printf
            try:
                self._httpd = subprocess.Popen(
                    command,
                    # don't care about setting stdout and stderr,
                    # we're in test mode anyway
                    shell=True,
                    executable=None,
                    # "preexec_fn" is UNIX specific, but so is "command"
                    preexec_fn=os.setsid)
            except OSError as error:  # ValueError should not happen!
                logger.debug(
                    "Couldn't execute manual command: %s", error, exc_info=True)
                return False
            logger.debug("Manual command running as PID %s.", self._httpd.pid)
            # give it some time to bootstrap, before we try to verify
            # (cert generation in case of simpleHttpS might take time)
            self._test_mode_busy_wait(port)

            if self._httpd.poll() is not None:
                raise errors.Error("Couldn't execute manual command")
        else:
            if self._is_text_mode():
                self._notify_and_wait(
                    self._get_message(achall).format(
                        validation=validation,
                        response=response,
                        uri=achall.chall.uri(achall.domain),
                        command=command))

            elif self._is_json_mode():
                self._json_out_and_wait(json_data)

            elif self._is_handler_mode():
                self._json_out(json_data, True)
                if self._call_handler("perform", **(self._get_json_to_kwargs(json_data))) is None:
                    raise errors.PluginError("Error in calling the handler to do the perform (challenge) stage")

            else:
                raise errors.PluginError("Unknown plugin mode selected")

        if not response.simple_verify(
                achall.chall, achall.domain,
                achall.account_key.public_key(), self.config.http01_port):
            logger.warning("Self-verify of challenge failed.")

        return response

    def _perform_dns01_challenge(self, achall):
        response, validation = achall.response_and_validation()

        json_data = OrderedDict()
        json_data[FIELD_CMD] = COMMAND_PERFORM
        json_data[FIELD_TYPE] = achall.chall.typ
        json_data[FIELD_DOMAIN] = achall.domain
        json_data[FIELD_TOKEN] = b64.b64encode(achall.chall.token)
        json_data[FIELD_VALIDATION] = validation
        json_data[FIELD_TXT_DOMAIN] = achall.validation_domain_name(achall.domain)
        json_data[FIELD_KEY_AUTH] = response.key_authorization

        json_data = self._json_sanitize_dict(json_data)

        if not self.conf("test-mode"):
            if self._is_text_mode():
                self._notify_and_wait(
                    self._get_message(achall).format(
                        validation=json_data[FIELD_VALIDATION],
                        domain=json_data[FIELD_DOMAIN],
                        response=response))

            elif self._is_json_mode():
                self._json_out_and_wait(json_data)

            elif self._is_handler_mode():
                self._json_out(json_data, True)
                if self._call_handler("perform", **(self._get_json_to_kwargs(json_data))) is None:
                    raise errors.PluginError("Error in calling the handler to do the perform (challenge) stage")

            else:
                raise errors.PluginError("Unknown plugin mode selected")

        try:
            verification_status = response.simple_verify(
                achall.chall, achall.domain,
                achall.account_key.public_key())
        except acme_errors.DependencyError:
            logger.warning("Self verification requires optional "
                           "dependency `dnspython` to be installed.")
        else:
            if not verification_status:
                logger.warning("Self-verify of challenge failed.")

        return response

    def _cleanup_http01_challenge(self, achall):
        # pylint: disable=missing-docstring,unused-argument
        if self.conf("test-mode"):
            assert self._httpd is not None, (
                "cleanup() must be called after perform()")
            if self._httpd.poll() is None:
                logger.debug("Terminating manual command process")
                os.killpg(self._httpd.pid, signal.SIGTERM)
            else:
                logger.debug("Manual command process already terminated "
                             "with %s code", self._httpd.returncode)
            shutil.rmtree(self._root)

    #
    # Installer section
    #

    def get_all_names(self):
        return []

    def deploy_cert(self, domain, cert_path, key_path, chain_path, fullchain_path):
        cur_record = OrderedDict()
        cur_record[FIELD_CMD] = COMMAND_DEPLOY_CERT
        cur_record[FIELD_DOMAIN] = domain
        cur_record[FIELD_CERT_PATH] = cert_path
        cur_record[FIELD_KEY_PATH] = key_path
        cur_record[FIELD_CHAIN_PATH] = chain_path
        cur_record[FIELD_FULLCHAIN_PATH] = fullchain_path
        cur_record[FIELD_TIMESTAMP] = self._start_time
        cur_record[FIELD_CERT_TIMESTAMP] = self._get_file_mtime(cert_path)

        if self._is_json_mode() or self._is_handler_mode():
            self._json_out(cur_record, True)

        hook_cmd = "deploy_cert" if cur_record[FIELD_CERT_TIMESTAMP] >= cur_record[FIELD_TIMESTAMP] else 'unchanged_cert'
        if self._is_handler_mode() and self._call_handler(hook_cmd, **(self._get_json_to_kwargs(cur_record))) is None:
            raise errors.PluginError("Error in calling the handler to do the deploy_cert stage")
        pass

    def enhance(self, domain, enhancement, options=None):
        pass  # pragma: no cover

    def supported_enhancements(self):
        return []

    def get_all_certs_keys(self):
        return []

    def save(self, title=None, temporary=False):
        cur_record = OrderedDict()
        cur_record[FIELD_CMD] = COMMAND_SAVE
        cur_record['title'] = title
        cur_record['temporary'] = temporary
        if self._is_json_mode() or self._is_handler_mode():
            self._json_out(cur_record, True)

    def rollback_checkpoints(self, rollback=1):
       pass  # pragma: no cover

    def recovery_routine(self):
        pass  # pragma: no cover

    def view_config_changes(self):
        pass  # pragma: no cover

    def config_test(self):
        pass  # pragma: no cover

    def restart(self):
        cur_record = OrderedDict()
        cur_record[FIELD_CMD] = COMMAND_RESTART
        if self._is_json_mode() or self._is_handler_mode():
            self._json_out(cur_record, True)

    #
    # Caller
    #

    def _call_handler(self, command, *args, **kwargs):
        """
        Invoking the handler script
        :param command:
        :param args:
        :param kwargs:
        :return:
        """
        env = dict(os.environ)
        env.update(kwargs)

        # Dehydrated compatibility mode - translate commands
        if self._is_dehydrated_dns():
            auth_cmd_map = {'perform': 'deploy_challenge', 'cleanup': 'clean_challenge'}
            install_cmd_map = {'deploy_cert': 'deploy_cert', 'unchanged_cert': 'unchanged_cert'}

            if command in auth_cmd_map:
                command = auth_cmd_map[command]
                args = list(args) + [kwargs.get(FIELD_DOMAIN), kwargs.get(FIELD_TOKEN), kwargs.get(FIELD_VALIDATION)]

            elif command in install_cmd_map:
                command = install_cmd_map[command]
                args = list(args) + [kwargs.get(FIELD_DOMAIN), kwargs.get(FIELD_KEY_PATH), kwargs.get(FIELD_CERT_PATH),
                                     kwargs.get(FIELD_FULLCHAIN_PATH), kwargs.get(FIELD_CHAIN_PATH)]

                if command == 'deploy_cert':
                    args.append(kwargs.get(FIELD_TIMESTAMP))

            else:
                logger.info("Dehydrated mode does not support this handler command: %s" % command)

        proc = None
        stdout, stderr = None, None
        arg_list = [self._get_handler(), command] + list(args)

        # Check if the handler exists
        if not os.path.isfile(self._get_handler()):
            self._handler_file_problem = True
            logger.error("Handler script file `%s` not found. Absolute path: %s"
                         % (self._get_handler(), self._try_get_abs_path(self._get_handler())))

            if os.path.exists(self._get_handler()):
                logger.error("Handler script `%s` is not a file" % self._get_handler())

            return None

        # Check if is executable
        # Still try to run, throw an exception only if problem really did occur.
        exec_problem = not self._is_file_executable(self._get_handler())

        # The handler invocation
        try:
            proc = subprocess.Popen(arg_list,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    env=env)
            stdout, stderr = proc.communicate()

            # Handler processing
            if proc.returncode != 0:
                if stdout.strip() == "NotImplemented":
                    logger.warning("Handler script does not implement the command %s\n - Stderr: \n%s",
                                   command, stderr)
                    return NotImplemented

                else:
                    logger.error("Handler script failed!\n - Stdout: \n%s\n - Stderr: \n%s", stdout, stderr)
                    return None

            else:
                    logger.info("Handler output (%s):\n - Stdout: \n%s\n - Stderr: \n%s",
                                command, stdout, stderr)
            return stdout

        except Exception as e:
            self._handler_file_problem = True
            logger.error("Handler script invocation failed with an exception. \n - Script: %s\n - Exception: %s"
                         % (' '.join(arg_list), e))
            if exec_problem:
                logger.error("Handler script %s does not have the executable permission set so it cannot be executed. "
                             "\n - Try running: chmod +x \"%s\" " % (self._get_handler(), self._try_get_abs_path(self._get_handler())))
            else:
                logger.warning("Make sure the handler file exists and is executable (+x permission on a Posix system)")

    #
    # Helper methods & UI
    #

    def _json_sanitize_dict(self, dictionary):
        """
        Sanitizes dictionary prior JSON serialization, handles byte string serialization
        :param dictionary:
        :return:
        """
        for key, val in list(dictionary.items()):
            # Not highly efficient, would be neater to clean up FIELD_TOKEN.
            # But if any of the others turn to bytes in the future, this will solve it:
            if isinstance(key, bytes):
                del dictionary[key]
                key = key.decode('UTF-8')
                dictionary[key] = val

            if isinstance(val, bytes):
                dictionary[key] = val.decode('UTF-8')

            elif type(val) in (list, tuple):
                nval = []
                for item in val:
                    if isinstance(item, bytes):
                        item = item.decode('UTF-8')
                    nval.append(item)
                dictionary[key] = nval
        return dictionary

    def _is_file_executable(self, fpath):
        """
        Returns true if the given file is executable (+x flag)
        :param fpath:
        :return:
        """
        if os.name.lower() == 'posix':
            try:
                return os.access(fpath, os.X_OK)
            except:
                return False
        else:
            return True

    def _try_get_abs_path(self, fpath):
        """
        Returns absolute path, catching possible exceptions.
        :param fpath:
        :return:
        """
        try:
            return os.path.abspath(fpath)
        except:
            return fpath

    def _get_file_mtime(self, file):
        """
        Returns file modification time
        :param file:
        :return:
        """
        return os.path.getmtime(file)

    def _is_text_mode(self):
        """
        Returns true if text-mode is selected
        :return:
        """
        return self.conf("text-mode")

    def _is_json_mode(self):
        """
        Returns true if json mode is selected
        :return:
        """
        return not self._is_text_mode() and not self._is_handler_mode()

    def _is_handler_mode(self):
        """
        Returns true if handler mode is selected
        :return:
        """
        return self.conf("handler") is not None

    def _is_handler_broken(self):
        """
        Returns true if the handler file cannot be executed - exception was thrown
        :return:
        """
        return self._handler_file_problem

    def _is_classic_handler_mode(self):
        """
        Handler mode && not dehydrated
        :return:
        """
        return self._is_handler_mode() and not self._is_dehydrated_dns()

    def _get_handler(self):
        """
        Returns handler script path - from CLI argument
        :return:
        """
        return self.conf("handler")

    def _is_dehydrated_dns(self):
        """
        Returns true if dehydrated dns mode is used
        :return:
        """
        return self.conf("dehydrated-dns")

    def _json_dumps(self, data, **kwargs):
        """
        Dumps data to the json string
        Using custom serializer by default
        :param data:
        :param kwargs:
        :return:
        """
        kwargs.setdefault('cls', AutoJSONEncoder)
        return json.dumps(data, **kwargs)

    def _json_out(self, data, new_line=False):
        """
        Dumps data as JSON to the stdout
        :param data:
        :param new_line:
        :return:
        """
        # pylint: disable=no-self-use
        json_str = self._json_dumps(data)
        if new_line:
            json_str += '\n'
        sys.stdout.write(json_str)
        sys.stdout.flush()

    def _json_out_and_wait(self, data):
        """
        Dumps data as JSON to stdout and waits for prompt
        :param data:
        :return:
        """
        # pylint: disable=no-self-use
        self._json_out(data, True)
        six.moves.input("")

    def _notify_and_wait(self, message):
        """
        Writes message to the stdout and waits for user confirmation
        :param message:
        :return:
        """
        # pylint: disable=no-self-use
        sys.stdout.write(message)
        sys.stdout.write("Press ENTER to continue")
        sys.stdout.flush()
        six.moves.input("")

    def _get_ip_logging_permission(self):
        """
        Configures public ip logging config keys from the env.
        :return:
        """
        if self.config.noninteractive_mode and self.conf("public-ip-logging-ok"):
            self.config.namespace.certbot_external_auth_out_public_ip_logging_ok = True
            self.config.namespace.manual_public_ip_logging_ok = True
            return

        if self.config.noninteractive_mode or (self._is_json_mode() and not self.conf("public-ip-logging-ok")):
            raise errors.PluginError("Must agree to the public IP logging to proceed")

        if not (self.conf("test-mode") or self.conf("public-ip-logging-ok")):
            self.config.namespace.certbot_external_auth_out_public_ip_logging_ok = True
            self.config.namespace.manual_public_ip_logging_ok = True

    def _get_message(self, achall):
        """
        Retrieves text message to display for the challange from templates
        :param achall:
        :return:
        """
        # pylint: disable=no-self-use,unused-argument
        return self.MESSAGE_TEMPLATE.get(achall.chall.typ, "")

