# Copyright 2013, 2014 Philipp Winter <phw@nymity.ch>
#
# This file is part of exitmap.
#
# exitmap is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# exitmap is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with exitmap.  If not, see <http://www.gnu.org/licenses/>.

import os
import sys
import time
import socket
import string
import pkgutil
import argparse
import datetime
import random
import logging

import stem
import stem.connection
import stem.process
import stem.descriptor
from stem.control import Controller, EventType

import log
import error
import util
import exitselector

from eventhandler import EventHandler
from stats import Statistics

logger = log.get_logger()


def bootstrap_tor(temp_dir):
    """
    Invoke a Tor process which is subsequently used by exitmap.
    """

    logger.info("Attempting to invoke Tor process in directory \"%s\"." %
                 temp_dir)

    try:
        proc = stem.process.launch_tor_with_config(
            config={
                "SOCKSPort": "45678",
                "ControlPort": "45679",
                "DataDirectory": temp_dir,
                "CookieAuthentication": "1",
                "LearnCircuitBuildTimeout": "0",
                "CircuitBuildTimeout": "40",
                "__DisablePredictedCircuits": "1",
                "__LeaveStreamsUnattached": "1",
                "FetchHidServDescriptors": "0",
                "UseMicroDescriptors": "0",
            },
            timeout=90,
            take_ownership=True,
            completion_percent=80,
            init_msg_handler=lambda line: logger.debug("Tor says: %s" % line),
        )
    except OSError as err:
        logger.error("Could not start Tor because: %s" % err)
        exit(1)

    logger.info("Successfully started Tor process (PID=%d)." % proc.pid)

    return proc


def parse_cmd_args():
    """
    Parse and return command line arguments.
    """

    parser = argparse.ArgumentParser(description="Perform a task over (a "
                                                 "subset of) all Tor exit "
                                                 "relays.")

    group = parser.add_mutually_exclusive_group()

    group.add_argument("-C", "--country", type=str, default=None,
                       help="Only probe exit relays of the country which is "
                            "determined by the given 2-letter country code.")

    group.add_argument("-e", "--exit", type=str, default=None,
                       help="Only probe the exit relay which has the given "
                            "20-byte fingerprint.")

    parser.add_argument("-c", "--consensus", type=str, default="",
                        help="Path to a Tor network consensus.")

    parser.add_argument("-d", "--build-delay", type=float, default=3,
                        help="Wait for the given delay (in seconds) between "
                             "circuit builds.  The default is 3.")

    parser.add_argument("-t", "--temp-dir", type=str,
                        default="/tmp/exitmap_tor_datadir",
                        help="Directory for temporary data.  If set, the "
                             "network consensus can be re-used in between "
                             "scans.")

    parser.add_argument("-v", "--verbosity", type=str, default="info",
                        help="Minimum verbosity level for logging.  Available "
                             "in ascending order: debug, info, warning, "
                             "error, critical).  The default is info.")

    parser.add_argument("first_hop", type=str, default="",
                        help="The 20-byte fingerprint of the Tor relay which "
                             "is used as first hop.  This relay should be "
                             "under your control.")

    parser.add_argument("module", nargs='+',
                        help="Run the given module (available: %s)." %
                        ", ".join(get_modules()))

    return parser.parse_args()


def get_modules():
    """
    Return all modules located in "modules/".
    """

    return [name for _, name, _ in pkgutil.iter_modules(["modules"])]


def main():
    """
    The scanner's entry point.
    """

    stats = Statistics()
    args = parse_cmd_args()

    logger.setLevel(logging.__dict__[string.upper(args.verbosity)])

    logger.debug("Command line arguments: %s" % str(args))

    bootstrap_tor(args.temp_dir)
    controller = Controller.from_port(port=45679)
    stem.connection.authenticate(controller)

    # Redirect Tor's logging to work around the following problem:
    # https://bugs.torproject.org/9862

    logger.debug("Redirecting Tor's logging to /dev/null.")
    controller.set_conf("Log", "err file /dev/null")

    # We already have the current consensus, so we don't need additional
    # descriptors or the streams fetching them.

    controller.set_conf("FetchServerDescriptors", "0")

    if not util.relay_in_consensus(args.first_hop,
                                   util.get_consensus_path(args)):
        logger.error("Given first hop \"%s\" not found in consensus.  Is it " \
                     "offline?" % args.first_hop)
        return 1

    for module_name in args.module:
        run_module(module_name, args, controller, stats)

    return 0


def select_exits(args, module):
    """
    Select exit relays which allow exiting to the module's scan destinations.

    We select exit relays based on their published exit policy.  In particular,
    we check if the exit relay's exit policy specifies that we can connect to
    our intended destination(s).
    """

    before = datetime.datetime.now()
    hosts = []

    consensus = util.get_consensus_path(args)

    if not os.path.exists(consensus):
        logger.critical("The consensus \"%s\" does not exist." % consensus)
        exit(1)

    if module.destinations is not None:
        hosts = [(socket.gethostbyname(host), port) for
                 (host, port) in module.destinations]

    # '-e' was used to specify a single exit relay.

    if args.exit:
        exit_relays = [args.exit]
        total = len(exit_relays)
    else:
        total, exit_relays = exitselector.get_exits(consensus,
                                                    country_code=args.country,
                                                    hosts=hosts)

    logger.debug("Successfully selected exit relays after %s." %
                 str(datetime.datetime.now() - before))

    logger.info("%d%s exits out of all %s exit relays allow exiting to %s." %
                (len(exit_relays), " %s" % args.country if args.country else "",
                 total, hosts))

    assert isinstance(exit_relays, list)

    random.shuffle(exit_relays)

    return exit_relays


def run_module(module_name, args, controller, stats):
    """
    Run an exitmap module over all available exit relays.
    """

    logger.info("Running module '%s'." % module_name)
    try:
        module = __import__("modules.%s" % module_name, fromlist=[module_name])
    except ImportError as err:
        logger.error("Failed to load module because: %s" % err)
        return

    exit_relays = select_exits(args, module)

    count = len(exit_relays)
    stats.total_circuits += count

    if count < 1:
        raise error.ExitSelectionError("Exit selection yielded %d exits "
                                       "but need at least one." % count)

    handler = EventHandler(controller, module.probe, stats)
    controller.add_event_listener(handler.new_event,
                                  EventType.CIRC, EventType.STREAM)

    logger.debug("Circuit creation delay of %.3f seconds will account for "
                 "total delay of %.3f seconds." % (
                     args.build_delay,
                     count * args.build_delay))

    # Start building a circuit for every exit relay we got.

    before = datetime.datetime.now()
    logger.debug("Beginning to trigger %d circuit creation(s)." % count)

    for exit_relay in exit_relays:
        try:
            controller.new_circuit([args.first_hop] + [exit_relay])
        except stem.ControllerError as err:
            stats.failed_circuits += 1
            logger.warning("Circuit with exit relay \"%s\" could not be "
                           "created: %s" % (exit_relay, err))

        time.sleep(args.build_delay)

    logger.debug("Done triggering circuit creations after %s." %
                 str(datetime.datetime.now() - before))

    stats.modules_run += 1
