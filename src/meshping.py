#!/usr/bin/python
# -*- coding: utf-8 -*-
# kate: space-indent on; indent-width 4; replace-tabs on;

from __future__ import division

import os
import math
import socket
import os.path

import json

from oping     import PingObj, PingError
from optparse  import OptionParser
from redis     import StrictRedis
from threading import Thread
from time      import sleep, time

from prom import run_prom

INTERVAL = 30

FAC_15m = math.exp(-INTERVAL / (     15 * 60.))
FAC_6h  = math.exp(-INTERVAL / ( 6 * 60 * 60.))
FAC_24h = math.exp(-INTERVAL / (24 * 60 * 60.))


class MeshPing(object):
    def __init__(self, redis, timeout=1):
        self.targets = {}
        self.histograms = {}
        self.timeout  = timeout
        self.redis = redis

    def redis_load(self, addr, field):
        rds_value = self.redis.get("meshping:%s:%s:%s" % (socket.gethostname(), addr, field))
        if rds_value is None:
            return None
        return json.loads(rds_value)

    def run(self):
        pingobj = PingObj()
        pingobj.set_timeout(self.timeout)

        next_ping = time() + 0.1

        current_targets = set()

        while True:
            now = time()
            next_ping = now + 30

            unseen_targets = current_targets.copy()
            for target in self.redis.smembers("meshping:targets"):
                if target not in current_targets:
                    current_targets.add(target)
                    name, addr = target.split("@", 1)
                    pingobj.add_host(addr)

                    self.targets[addr] = self.redis_load(addr, "target") or {
                        "name": name, "addr": addr,
                        "sent": 0, "lost": 0, "recv": 0, "last": 0, "sum":  0, "min":  0, "max":  0
                    }
                    histogram = self.redis_load(addr, "histogram") or {}
                    # json sucks and converts dict keys to strings
                    histogram = dict([(int(x), y) for (x, y) in histogram.items()])
                    self.histograms[addr] = histogram

                else:
                    unseen_targets.remove(target)

            for target in unseen_targets:
                current_targets.remove(target)
                name, addr = target.split("@", 1)
                try:
                    pingobj.remove_host(addr)
                except PingError:
                    # Host probably not there anyway
                    pass
                self.targets.pop(addr, None)
                self.histograms.pop(addr, None)

            pingobj.send()

            rdspipe = self.redis.pipeline()

            for hostinfo in pingobj.get_hosts():
                target = self.targets[hostinfo["addr"]]
                histogram  = self.histograms.setdefault(hostinfo["addr"], {})

                target["sent"] += 1

                if hostinfo["latency"] != -1:
                    target["recv"] += 1
                    target["last"]  = hostinfo["latency"]
                    target["sum"]  += target["last"]
                    target["max"]   = max(target["max"], target["last"])

                    if target["min"] == 0:
                        target["min"] = target["last"]
                    else:
                        target["min"] = min(target["min"], target["last"])

                    if "avg15m" not in target:
                        target["avg15m"] = target["last"]
                    else:
                        target["avg15m"] = (target["avg15m"] * FAC_15m) + (target["last"] * (1 - FAC_15m))

                    if "avg6h" not in target:
                        target["avg6h"] = target["last"]
                    else:
                        target["avg6h"] = (target["avg6h"] * FAC_6h) + (target["last"] * (1 - FAC_6h))

                    if "avg24h" not in target:
                        target["avg24h"] = target["last"]
                    else:
                        target["avg24h"] = (target["avg24h"] * FAC_24h) + (target["last"] * (1 - FAC_24h))

                    histbucket = int(math.log(hostinfo["latency"], 2) * 10)
                    histogram.setdefault(histbucket, 0)
                    histogram[histbucket] += 1

                else:
                    target["lost"] += 1

                rds_prefix = "meshping:%s:%s" % (socket.gethostname(), target["addr"])
                rdspipe.setex("%s:target"    % rds_prefix, 7 * 86400, json.dumps(target))
                rdspipe.setex("%s:histogram" % rds_prefix, 7 * 86400, json.dumps(histogram))

            rdspipe.execute()

            sleep(next_ping - time())

def main():
    if os.getuid() != 0:
        raise RuntimeError("need to be root, sorry about that")

    ctrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.SOL_UDP)
    ctrl.bind(("127.0.0.1", 55432))

    parser = OptionParser("Usage: %prog [options] <target ...>")
    parser.add_option(
        "-t", "--timeout",  help="Ping timeout [5s]", type=int, default=5
    )
    parser.add_option(
        "-r", "--redishost",  help="Redis Host [127.0.0.1]", default="127.0.0.1"
    )
    options, posargs = parser.parse_args()

    redis = StrictRedis(host=options.redishost)
    mp = MeshPing(redis, options.timeout)

    for target in posargs:
        if "@" not in target:
            for info in socket.getaddrinfo(target, 0, 0, socket.SOCK_STREAM):
                redis.sadd("meshping:targets", "%s@%s" % (target, info[4][0]))
        else:
            redis.sadd("meshping:targets", target)

    promrunner = Thread(target=run_prom, args=(mp,))
    promrunner.daemon = True
    promrunner.start()

    try:
        mp.run()
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    main()
