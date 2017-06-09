
"""
Demonstrate writing a Ceph web interface inside a mgr module.
"""

# We must share a global reference to this instance, because it is the
# gatekeeper to all accesses to data from the C++ side (e.g. the REST API
# request handlers need to see it)
from collections import defaultdict
import collections

_global_instance = {'plugin': None}
def global_instance():
    assert _global_instance['plugin'] is not None
    return _global_instance['plugin']


import os
import logging
import logging.config
import json
import sys
import time
import threading

import cherrypy
import jinja2

from mgr_module import MgrModule, CommandResult

from types import OsdMap, NotFound, Config, FsMap, MonMap, \
    PgSummary, Health, MonStatus

import rados
from rbd_ls import RbdLs
from cephfs_clients import CephFSClients


log = logging.getLogger("dashboard")


# How many cluster log lines shall we hold onto in our
# python module for the convenience of the GUI?
LOG_BUFFER_SIZE = 30

# cherrypy likes to sys.exit on error.  don't let it take us down too!
def os_exit_noop():
    pass

os._exit = os_exit_noop


def recurse_refs(root, path):
    if isinstance(root, dict):
        for k, v in root.items():
            recurse_refs(v, path + "->%s" % k)
    elif isinstance(root, list):
        for n, i in enumerate(root):
            recurse_refs(i, path + "[%d]" % n)

    log.info("%s %d (%s)" % (path, sys.getrefcount(root), root.__class__))


class Module(MgrModule):
    def __init__(self, *args, **kwargs):
        super(Module, self).__init__(*args, **kwargs)
        _global_instance['plugin'] = self
        self.log.info("Constructing module {0}: instance {1}".format(
            __name__, _global_instance))

        self.log_buffer = collections.deque(maxlen=LOG_BUFFER_SIZE)

        # Keep a librados instance for those that need it.
        self._rados = None

        # Stateful instances of RbdLs, hold cached results.  Key to dict
        # is pool name.
        self.rbd_ls = {}

        # Stateful instances of CephFSClients, hold cached results.  Key to
        # dict is FSCID
        self.cephfs_clients = {}

        # A short history of pool df stats
        self.pool_stats = defaultdict(lambda: defaultdict(
            lambda: collections.deque(maxlen=10)))

    @property
    def rados(self):
        """
        A librados instance to be shared by any classes within
        this mgr module that want one.
        """
        if self._rados:
            return self._rados

        from mgr_module import ceph_state
        ctx_capsule = ceph_state.get_context()
        self._rados = rados.Rados(context=ctx_capsule)
        self._rados.connect()

        return self._rados

    def get_localized_config(self, key):
        r = self.get_config(self.get_mgr_id() + '/' + key)
        if r is None:
            r = self.get_config(key)
        return r

    def update_pool_stats(self):
        df = global_instance().get("df")
        pool_stats = dict([(p['id'], p['stats']) for p in df['pools']])
        now = time.time()
        for pool_id, stats in pool_stats.items():
            for stat_name, stat_val in stats.items():
                self.pool_stats[pool_id][stat_name].appendleft((now, stat_val))

    def notify(self, notify_type, notify_val):
        if notify_type == "clog":
            log.info("clog: {0}".format(notify_val["message"]))
            log.info("clog: {0}".format(json.dumps(notify_val)))
            self.log_buffer.appendleft(notify_val)
        elif notify_type == "pg_summary":
            self.update_pool_stats()
        else:
            pass

    def get_sync_object(self, object_type, path=None):
        if object_type == OsdMap:
            data = self.get("osd_map")

            assert data is not None

            data['tree'] = self.get("osd_map_tree")
            data['crush'] = self.get("osd_map_crush")
            data['crush_map_text'] = self.get("osd_map_crush_map_text")
            data['osd_metadata'] = self.get("osd_metadata")
            obj = OsdMap(data)
        elif object_type == Config:
            data = self.get("config")
            obj = Config( data)
        elif object_type == MonMap:
            data = self.get("mon_map")
            obj = MonMap(data)
        elif object_type == FsMap:
            data = self.get("fs_map")
            obj = FsMap(data)
        elif object_type == PgSummary:
            data = self.get("pg_summary")
            self.log.debug("JSON: {0}".format(data))
            obj = PgSummary(data)
        elif object_type == Health:
            data = self.get("health")
            obj = Health(json.loads(data['json']))
        elif object_type == MonStatus:
            data = self.get("mon_status")
            obj = MonStatus(json.loads(data['json']))
        else:
            raise NotImplementedError(object_type)

        # TODO: move 'path' handling up into C++ land so that we only
        # Pythonize the part we're interested in
        if path:
            try:
                for part in path:
                    if isinstance(obj, dict):
                        obj = obj[part]
                    else:
                        obj = getattr(obj, part)
            except (AttributeError, KeyError):
                raise NotFound(object_type, path)

        return obj

    def shutdown(self):
        log.info("Stopping server...")
        cherrypy.engine.exit()
        log.info("Stopped server")

        log.info("Stopping librados...")
        if self._rados:
            self._rados.shutdown()
        log.info("Stopped librados.")

    def get_latest(self, daemon_type, daemon_name, stat):
        data = self.get_counter(daemon_type, daemon_name, stat)[stat]
        if data:
            return data[-1][1]
        else:
            return 0

    def get_rate(self, daemon_type, daemon_name, stat):
        data = self.get_counter(daemon_type, daemon_name, stat)[stat]

        if data and len(data) > 1:
            return (data[-1][1] - data[-2][1]) / float(data[-1][0] - data[-2][0])
        else:
            return 0

    def format_dimless(self, n, width, colored=True):
        """
        Format a number without units, so as to fit into `width` characters, substituting
        an appropriate unit suffix.
        """
        units = [' ', 'k', 'M', 'G', 'T', 'P']
        unit = 0
        while len("%s" % (int(n) // (1000**unit))) > width - 1:
            unit += 1

        if unit > 0:
            truncated_float = ("%f" % (n / (1000.0 ** unit)))[0:width - 1]
            if truncated_float[-1] == '.':
                truncated_float = " " + truncated_float[0:-1]
        else:
            truncated_float = "%{wid}d".format(wid=width-1) % n
        formatted = "%s%s" % (truncated_float, units[unit])

        if colored:
            # TODO: html equivalent
            # if n == 0:
            #     color = self.BLACK, False
            # else:
            #     color = self.YELLOW, False
            # return self.bold(self.colorize(formatted[0:-1], color[0], color[1])) \
            #     + self.bold(self.colorize(formatted[-1], self.BLACK, False))
            return formatted
        else:
            return formatted

    def fs_status(self, fs_id):
        mds_versions = defaultdict(list)

        fsmap = self.get("fs_map")
        filesystem = None
        for fs in fsmap['filesystems']:
            if fs['id'] == fs_id:
                filesystem = fs
                break

        rank_table = []

        mdsmap = filesystem['mdsmap']

        client_count = 0

        for rank in mdsmap["in"]:
            up = "mds_{0}".format(rank) in mdsmap["up"]
            if up:
                gid = mdsmap['up']["mds_{0}".format(rank)]
                info = mdsmap['info']['gid_{0}'.format(gid)]
                dns = self.get_latest("mds", info['name'], "mds.inodes")
                inos = self.get_latest("mds", info['name'], "mds_mem.ino")

                if rank == 0:
                    client_count = self.get_latest("mds", info['name'],
                                                   "mds_sessions.session_count")
                elif client_count == 0:
                    # In case rank 0 was down, look at another rank's
                    # sessionmap to get an indication of clients.
                    client_count = self.get_latest("mds", info['name'],
                                                   "mds_sessions.session_count")

                laggy = "laggy_since" in info

                state = info['state'].split(":")[1]
                if laggy:
                    state += "(laggy)"

                # if state == "active" and not laggy:
                #     c_state = self.colorize(state, self.GREEN)
                # else:
                #     c_state = self.colorize(state, self.YELLOW)

                # Populate based on context of state, e.g. client
                # ops for an active daemon, replay progress, reconnect
                # progress
                activity = ""

                if state == "active":
                    activity = "Reqs: " + self.format_dimless(
                        self.get_rate("mds", info['name'], "mds_server.handle_client_request"),
                        5
                    ) + "/s"

                metadata = self.get_metadata('mds', info['name'])
                mds_versions[metadata['ceph_version']].append(info['name'])
                rank_table.append(
                    {
                        "rank": rank,
                        "state": state,
                        "mds": info['name'],
                        "activity": activity,
                        "dns": dns,
                        "inos": inos
                    }
                )

            else:
                rank_table.append(
                    {
                        "rank": rank,
                        "state": "failed",
                        "mds": "",
                        "activity": "",
                        "dns": 0,
                        "inos": 0
                    }
                )

        # Find the standby replays
        for gid_str, daemon_info in mdsmap['info'].iteritems():
            if daemon_info['state'] != "up:standby-replay":
                continue

            inos = self.get_latest("mds", daemon_info['name'], "mds_mem.ino")
            dns = self.get_latest("mds", daemon_info['name'], "mds.inodes")

            activity = "Evts: " + self.format_dimless(
                self.get_rate("mds", daemon_info['name'], "mds_log.replay"),
                5
            ) + "/s"

            rank_table.append(
                {
                    "rank": "{0}-s".format(daemon_info['rank']),
                    "state": "standby-replay",
                    "mds": daemon_info['name'],
                    "activity": activity,
                    "dns": dns,
                    "inos": inos
                }
            )

        df = self.get("df")
        pool_stats = dict([(p['id'], p['stats']) for p in df['pools']])
        osdmap = self.get("osd_map")
        pools = dict([(p['pool'], p) for p in osdmap['pools']])
        metadata_pool_id = mdsmap['metadata_pool']
        data_pool_ids = mdsmap['data_pools']

        pools_table = []
        for pool_id in [metadata_pool_id] + data_pool_ids:
            pool_type = "metadata" if pool_id == metadata_pool_id else "data"
            stats = pool_stats[pool_id]
            pools_table.append({
                "pool": pools[pool_id]['pool_name'],
                "type": pool_type,
                "used": stats['bytes_used'],
                "avail": stats['max_avail']
            })

        standby_table = []
        for standby in fsmap['standbys']:
            metadata = self.get_metadata('mds', standby['name'])
            mds_versions[metadata['ceph_version']].append(standby['name'])

            standby_table.append({
                'name': standby['name']
            })

        return {
            "filesystem": {
                "id": fs_id,
                "name": mdsmap['fs_name'],
                "client_count": client_count,
                "clients_url": "/clients/{0}/".format(fs_id),
                "ranks": rank_table,
                "pools": pools_table
            },
            "standbys": standby_table,
            "versions": mds_versions
        }

    def serve(self):
        current_dir = os.path.dirname(os.path.abspath(__file__))

        jinja_loader = jinja2.FileSystemLoader(current_dir)
        env = jinja2.Environment(loader=jinja_loader)

        class Root(object):
            def _toplevel_data(self):
                """
                Data consumed by the base.html template
                """
                fsmap = global_instance().get_sync_object(FsMap)
                filesystems = [
                    {
                        "id": f['id'],
                        "name": f['mdsmap']['fs_name'],
                        "url": "/filesystem/{0}/".format(f['id'])
                    }
                    for f in fsmap.data['filesystems']
                ]

                return {
                    'health': global_instance().get_sync_object(Health).data,
                    'filesystems': filesystems
                }

            @cherrypy.expose
            def filesystem(self, fs_id):
                template = env.get_template("filesystem.html")

                toplevel_data = self._toplevel_data()

                content_data = {
                    "fs_status": global_instance().fs_status(int(fs_id))
                }

                return template.render(
                    ceph_version=global_instance().version,
                    toplevel_data=json.dumps(toplevel_data, indent=2),
                    content_data=json.dumps(content_data, indent=2)
                )

            @cherrypy.expose
            @cherrypy.tools.json_out()
            def filesystem_data(self, fs_id):
                return global_instance().fs_status(int(fs_id))

            def _osd(self, osd_id):
                #global_instance().fs_status(int(fs_id))
                osd_id = int(osd_id)

                osd_map = global_instance().get("osd_map")

                osd = None
                for o in osd_map['osds']:
                    if o['osd'] == osd_id:
                        osd = o
                        break

                assert osd is not None  # TODO 400

                osd_spec = "{0}".format(osd_id) 

                osd_metadata = global_instance().get_metadata(
                        "osd", osd_spec)

                result = CommandResult("")
                global_instance().send_command(result, "osd", osd_spec,
                       json.dumps({
                           "prefix": "perf histogram dump",
                           }),
                       "")
                r, outb, outs = result.wait()
                assert r == 0
                histogram = json.loads(outb)

                return {
                    "osd": osd,
                    "osd_metadata": osd_metadata,
                    "osd_histogram": histogram
                }

            @cherrypy.expose
            def osd_perf(self, osd_id):
                template = env.get_template("osd_perf.html")
                toplevel_data = self._toplevel_data()

                return template.render(
                    ceph_version=global_instance().version,
                    toplevel_data=json.dumps(toplevel_data, indent=2),
                    content_data=json.dumps(self._osd(osd_id), indent=2)
                )

            @cherrypy.expose
            @cherrypy.tools.json_out()
            def osd_perf_data(self, osd_id):
                return self._osd(osd_id)

            def _clients(self, fs_id):
                cephfs_clients = global_instance().cephfs_clients.get(fs_id, None)
                if cephfs_clients is None:
                    cephfs_clients = CephFSClients(global_instance(), fs_id)
                    global_instance().cephfs_clients[fs_id] = cephfs_clients

                status, clients = cephfs_clients.get()
                #TODO do something sensible with status

                # Decorate the metadata with some fields that will be
                # indepdendent of whether it's a kernel or userspace
                # client, so that the javascript doesn't have to grok that.
                for client in clients:
                    if "ceph_version" in client['client_metadata']:
                        client['type'] = "userspace"
                        client['version'] = client['client_metadata']['ceph_version']
                        client['hostname'] = client['client_metadata']['hostname']
                    elif "kernel_version" in client['client_metadata']:
                        client['type'] = "kernel"
                        client['version'] = client['kernel_version']
                        client['hostname'] = client['client_metadata']['hostname']
                    else:
                        client['type'] = "unknown"
                        client['version'] = ""
                        client['hostname'] = ""

                return clients

            @cherrypy.expose
            def clients(self, fs_id):
                template = env.get_template("clients.html")

                toplevel_data = self._toplevel_data()

                clients = self._clients(int(fs_id))
                global_instance().log.debug(json.dumps(clients, indent=2))
                content_data = {
                    "clients": clients,
                    "fscid": fs_id
                }

                return template.render(
                    ceph_version=global_instance().version,
                    toplevel_data=json.dumps(toplevel_data, indent=2),
                    content_data=json.dumps(content_data, indent=2)
                )

            @cherrypy.expose
            @cherrypy.tools.json_out()
            def clients_data(self, fs_id):
                return self._clients(int(fs_id))

            def _rbd(self, pool_name):
                rbd_ls = global_instance().rbd_ls.get(pool_name, None)
                if rbd_ls is None:
                    rbd_ls = RbdLs(global_instance(), pool_name)
                    global_instance().rbd_ls[pool_name] = rbd_ls

                status, value = rbd_ls.get()

                interval = 5

                wait = interval - rbd_ls.latency
                def wait_and_load():
                    time.sleep(wait)
                    rbd_ls.get()

                threading.Thread(target=wait_and_load).start()

                assert status != RbdLs.VALUE_NONE  # FIXME bubble status up to UI
                return value

            @cherrypy.expose
            def rbd(self, pool_name):
                template = env.get_template("rbd.html")

                toplevel_data = self._toplevel_data()

                images = self._rbd(pool_name)
                content_data = {
                    "images": images,
                    "pool_name": pool_name
                }

                return template.render(
                    ceph_version=global_instance().version,
                    toplevel_data=json.dumps(toplevel_data, indent=2),
                    content_data=json.dumps(content_data, indent=2)
                )

            @cherrypy.expose
            @cherrypy.tools.json_out()
            def rbd_data(self, pool_name):
                return self._rbd(pool_name)

            @cherrypy.expose
            def health(self):
                template = env.get_template("health.html")
                return template.render(
                    ceph_version=global_instance().version,
                    toplevel_data=json.dumps(self._toplevel_data(), indent=2),
                    content_data=json.dumps(self._health(), indent=2)
                )

            @cherrypy.expose
            def servers(self):
                template = env.get_template("servers.html")
                return template.render(
                    ceph_version=global_instance().version,
                    toplevel_data=json.dumps(self._toplevel_data(), indent=2),
                    content_data=json.dumps(self._servers(), indent=2)
                )

            def _servers(self):
                servers = global_instance().list_servers()
                return {
                    'servers': global_instance().list_servers()
                }

            @cherrypy.expose
            @cherrypy.tools.json_out()
            def servers_data(self):
                return self._servers()

            def _health(self):
                # Fuse osdmap with pg_summary to get description of pools
                # including their PG states
                osd_map = global_instance().get_sync_object(OsdMap).data
                pg_summary = global_instance().get_sync_object(PgSummary).data
                pools = []

                if len(global_instance().pool_stats) == 0:
                    global_instance().update_pool_stats()

                for pool in osd_map['pools']:
                    pool['pg_status'] = pg_summary['by_pool'][pool['pool'].__str__()]
                    stats = global_instance().pool_stats[pool['pool']]
                    s = {}

                    def get_rate(series):
                        if len(series) >= 2:
                            return (series[0][1] - series[1][1]) / float(series[0][0] - series[1][0])
                        else:
                            return 0

                    for stat_name, stat_series in stats.items():
                        s[stat_name] = {
                            'latest': stat_series[0][1],
                            'rate': get_rate(stat_series),
                            'series': [i for i in stat_series]
                        }
                    pool['stats'] = s
                    pools.append(pool)

                # Not needed, skip the effort of transmitting this
                # to UI
                del osd_map['pg_temp']

                return {
                    "health": global_instance().get_sync_object(Health).data,
                    "mon_status": global_instance().get_sync_object(
                        MonStatus).data,
                    "osd_map": osd_map,
                    "clog": list(global_instance().log_buffer),
                    "pools": pools
                }

            @cherrypy.expose
            @cherrypy.tools.json_out()
            def health_data(self):
                return self._health()

            @cherrypy.expose
            def index(self):
                return self.health()

            @cherrypy.expose
            @cherrypy.tools.json_out()
            def toplevel_data(self):
                return self._toplevel_data()

            def _get_mds_names(self, filesystem_id=None):
                names = []

                fsmap = global_instance().get("fs_map")
                for fs in fsmap['filesystems']:
                    if filesystem_id is not None and fs['id'] != filesystem_id:
                        continue
                    names.extend([info['name'] for _, info in fs['mdsmap']['info'].items()])

                if filesystem_id is None:
                    names.extend(info['name'] for info in fsmap['standbys'])

                return names

            @cherrypy.expose
            @cherrypy.tools.json_out()
            def mds_counters(self, fs_id):
                """
                Result format: map of daemon name to map of counter to list of datapoints
                """

                # Opinionated list of interesting performance counters for the GUI --
                # if you need something else just add it.  See how simple life is
                # when you don't have to write general purpose APIs?
                counters = [
                    "mds_server.handle_client_request",
                    "mds_log.ev",
                    "mds_cache.num_strays",
                    "mds.exported",
                    "mds.exported_inodes",
                    "mds.imported",
                    "mds.imported_inodes",
                    "mds.inodes",
                    "mds.caps",
                    "mds.subtrees"
                ]

                result = {}
                mds_names = self._get_mds_names(int(fs_id))

                for mds_name in mds_names:
                    result[mds_name] = {}
                    for counter in counters:
                        data = global_instance().get_counter("mds", mds_name, counter)
                        if data is not None:
                            result[mds_name][counter] = data[counter]
                        else:
                            result[mds_name][counter] = []

                return dict(result)

        server_addr = self.get_localized_config('server_addr') or '127.0.0.1'
        server_port = self.get_localized_config('server_port') or '7000'
        log.info("server_addr: %s server_port: %s" % (server_addr, server_port))
        cherrypy.config.update({
            'server.socket_host': server_addr,
            'server.socket_port': int(server_port),
            'engine.autoreload.on': False
        })

        static_dir = os.path.join(current_dir, 'static')
        conf = {
            "/static": {
                "tools.staticdir.on": True,
                'tools.staticdir.dir': static_dir
            }
        }
        log.info("Serving static from {0}".format(static_dir))
        cherrypy.tree.mount(Root(), "/", conf)

        log.info("Starting engine...")
        cherrypy.engine.start()
        log.info("Waiting for engine...")
        cherrypy.engine.block()
        log.info("Engine done.")
