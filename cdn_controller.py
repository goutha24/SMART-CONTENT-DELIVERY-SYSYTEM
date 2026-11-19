#!/usr/bin/env python3
"""
Ryu SDN Controller for Smart Content Delivery System.

Features:
- Topology discovery & shortest-path routing (Dijkstra)
- Maintains server health / load (via periodic status or probing)
- Dynamically selects the best content server for client requests
  arriving at the virtual IP 10.0.0.100:5000
- Installs / updates OpenFlow rules to redirect traffic
- Detects link failures and recomputes paths
- Supports manual failure injection via REST-like simple API on port 8080
- Collects basic stats for performance evaluation

Run with:
  ryu-manager --observe-links controller/cdn_controller.py
"""

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import CONFIG_DISPATCHER, MAIN_DISPATCHER, DEAD_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_3
from ryu.lib.packet import packet, ethernet, ether_types, ipv4, tcp, arp
from ryu.lib import hub
from ryu.topology import event as topo_event
from ryu.topology.api import get_switch, get_link, get_host
from ryu.app.wsgi import ControllerBase, WSGIApplication, route
from ryu.lib import dpid as dpid_lib

import networkx as nx
import json
import time
import socket
from webob import Response

CONTENT_VIP = "10.0.0.100"
CONTENT_PORT = 5000

# Known content servers (IP -> metadata)
SERVERS = {
    "10.0.0.11": {"id": "srv1", "mac": "00:00:00:00:00:11", "port": 5000, "load": 0, "alive": True},
    "10.0.0.12": {"id": "srv2", "mac": "00:00:00:00:00:12", "port": 5000, "load": 0, "alive": True},
    "10.0.0.13": {"id": "srv3", "mac": "00:00:00:00:00:13", "port": 5000, "load": 0, "alive": True},
}


class CDNController(app_manager.RyuApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super(CDNController, self).__init__(*args, **kwargs)
        self.mac_to_port = {}          # dpid -> {mac: port}
        self.datapaths = {}
        self.graph = nx.Graph()
        self.host_location = {}        # ip -> (dpid, port)
        self.server_selection = {}     # client_ip -> chosen_server_ip  (sticky)
        self.flow_stats = []
        self.start_time = time.time()

        wsgi = kwargs["wsgi"]
        wsgi.register(CDNRestController, {"cdn_app": self})

        # Background tasks
        self.monitor_thread = hub.spawn(self._monitor)
        self.topo_thread = hub.spawn(self._topo_discovery_loop)

        self.logger.info("CDN Controller started. VIP=%s:%d", CONTENT_VIP, CONTENT_PORT)

    # ------------------------------------------------------------------
    # Switch features / table-miss
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def switch_features_handler(self, ev):
        datapath = ev.msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id
        self.datapaths[dpid] = datapath
        self.mac_to_port.setdefault(dpid, {})

        # Table-miss -> controller
        match = parser.OFPMatch()
        actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER,
                                          ofproto.OFPCML_NO_BUFFER)]
        self.add_flow(datapath, 0, match, actions)
        self.logger.info("Switch %s connected", dpid)

    def add_flow(self, datapath, priority, match, actions, idle_timeout=0, hard_timeout=0):
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS, actions)]
        mod = parser.OFPFlowMod(
            datapath=datapath, priority=priority,
            match=match, instructions=inst,
            idle_timeout=idle_timeout, hard_timeout=hard_timeout
        )
        datapath.send_msg(mod)

    # ------------------------------------------------------------------
    # Packet-In
    # ------------------------------------------------------------------
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def packet_in_handler(self, ev):
        msg = ev.msg
        datapath = msg.datapath
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        in_port = msg.match["in_port"]
        dpid = datapath.id

        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth.ethertype == ether_types.ETH_TYPE_LLDP:
            return

        dst = eth.dst
        src = eth.src
        self.mac_to_port.setdefault(dpid, {})
        self.mac_to_port[dpid][src] = in_port

        # Learn host locations from ARP / IP
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        arp_pkt = pkt.get_protocol(arp.arp)

        if arp_pkt:
            self.host_location[arp_pkt.src_ip] = (dpid, in_port)
            # Answer ARP for VIP ourselves
            if arp_pkt.opcode == arp.ARP_REQUEST and arp_pkt.dst_ip == CONTENT_VIP:
                self._handle_vip_arp(datapath, in_port, eth, arp_pkt)
                return

        if ip_pkt:
            self.host_location[ip_pkt.src] = (dpid, in_port)

            # Intercept traffic destined to VIP:CONTENT_PORT
            if ip_pkt.dst == CONTENT_VIP:
                tcp_pkt = pkt.get_protocol(tcp.tcp)
                if tcp_pkt and tcp_pkt.dst_port == CONTENT_PORT:
                    self._handle_content_request(datapath, in_port, eth, ip_pkt, tcp_pkt, msg)
                    return

        # Normal L2 learning switch behaviour
        out_port = self.mac_to_port[dpid].get(dst, ofproto.OFPP_FLOOD)
        actions = [parser.OFPActionOutput(out_port)]

        # Install flow if we know the destination
        if out_port != ofproto.OFPP_FLOOD:
            match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
            self.add_flow(datapath, 1, match, actions, idle_timeout=30)

        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data
        )
        datapath.send_msg(out)

    def _handle_vip_arp(self, datapath, in_port, eth, arp_pkt):
        """Reply to ARP requests for the Content VIP with a synthetic MAC."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        # Use a fixed virtual MAC for the VIP
        vip_mac = "00:00:00:aa:bb:cc"

        # Build ARP reply
        from ryu.lib.packet import packet as pktlib
        e = ethernet.ethernet(dst=eth.src, src=vip_mac, ethertype=ether_types.ETH_TYPE_ARP)
        a = arp.arp(
            opcode=arp.ARP_REPLY,
            src_mac=vip_mac, src_ip=CONTENT_VIP,
            dst_mac=eth.src, dst_ip=arp_pkt.src_ip
        )
        p = pktlib.Packet()
        p.add_protocol(e)
        p.add_protocol(a)
        p.serialize()

        actions = [parser.OFPActionOutput(in_port)]
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=ofproto.OFP_NO_BUFFER,
            in_port=ofproto.OFPP_CONTROLLER, actions=actions, data=p.data
        )
        datapath.send_msg(out)
        self.logger.info("ARP reply for VIP %s -> %s (to %s)", CONTENT_VIP, vip_mac, arp_pkt.src_ip)

    def _handle_content_request(self, datapath, in_port, eth, ip_pkt, tcp_pkt, msg):
        """Select best server and install redirect flows (DNAT-like)."""
        client_ip = ip_pkt.src
        chosen = self._select_server(client_ip)
        if chosen is None:
            self.logger.warning("No alive server for client %s", client_ip)
            return

        server_info = SERVERS[chosen]
        self.logger.info("Client %s -> selected server %s (%s)",
                         client_ip, server_info["id"], chosen)

        # Install bidirectional rewrite rules on this switch and path
        self._install_redirect_flows(datapath, client_ip, chosen, server_info, in_port, eth, ip_pkt, tcp_pkt, msg)

    def _select_server(self, client_ip: str) -> str:
        """Choose the best alive server. Prefer sticky mapping, then lowest load + path cost."""
        # Sticky if still alive
        if client_ip in self.server_selection:
            prev = self.server_selection[client_ip]
            if prev in SERVERS and SERVERS[prev]["alive"]:
                return prev

        candidates = [(ip, info) for ip, info in SERVERS.items() if info["alive"]]
        if not candidates:
            return None

        # Simple scoring: load + estimated hop distance
        best = None
        best_score = float("inf")
        client_loc = self.host_location.get(client_ip)
        for ip, info in candidates:
            score = info["load"] * 10
            srv_loc = self.host_location.get(ip)
            if client_loc and srv_loc and self.graph.number_of_nodes() > 0:
                try:
                    path = nx.shortest_path(self.graph, client_loc[0], srv_loc[0])
                    score += len(path) - 1
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    score += 100
            if score < best_score:
                best_score = score
                best = ip

        if best:
            self.server_selection[client_ip] = best
        return best

    def _install_redirect_flows(self, datapath, client_ip, server_ip, server_info,
                                in_port, eth, ip_pkt, tcp_pkt, msg):
        """Install OpenFlow rules that rewrite VIP <-> real server IP/MAC."""
        ofproto = datapath.ofproto
        parser = datapath.ofproto_parser
        dpid = datapath.id

        vip_mac = "00:00:00:aa:bb:cc"
        server_mac = server_info["mac"]

        # Find out-port toward the server (simple: use learned MAC or flood)
        out_port = self.mac_to_port.get(dpid, {}).get(server_mac, ofproto.OFPP_FLOOD)

        # Client -> Server direction (rewrite dst IP/MAC)
        match_c2s = parser.OFPMatch(
            eth_type=0x0800,
            ipv4_src=client_ip,
            ipv4_dst=CONTENT_VIP,
            ip_proto=6,
            tcp_dst=CONTENT_PORT
        )
        actions_c2s = [
            parser.OFPActionSetField(eth_dst=server_mac),
            parser.OFPActionSetField(ipv4_dst=server_ip),
            parser.OFPActionOutput(out_port)
        ]
        self.add_flow(datapath, 100, match_c2s, actions_c2s, idle_timeout=60)

        # Server -> Client direction (rewrite src IP/MAC back to VIP)
        # We need the reverse out-port (toward client)
        client_mac = eth.src
        rev_out = self.mac_to_port.get(dpid, {}).get(client_mac, in_port)
        match_s2c = parser.OFPMatch(
            eth_type=0x0800,
            ipv4_src=server_ip,
            ipv4_dst=client_ip,
            ip_proto=6,
            tcp_src=CONTENT_PORT
        )
        actions_s2c = [
            parser.OFPActionSetField(eth_src=vip_mac),
            parser.OFPActionSetField(ipv4_src=CONTENT_VIP),
            parser.OFPActionOutput(rev_out)
        ]
        self.add_flow(datapath, 100, match_s2c, actions_s2c, idle_timeout=60)

        # Also send the current packet out after rewrite
        actions = [
            parser.OFPActionSetField(eth_dst=server_mac),
            parser.OFPActionSetField(ipv4_dst=server_ip),
            parser.OFPActionOutput(out_port)
        ]
        data = None
        if msg.buffer_id == ofproto.OFP_NO_BUFFER:
            data = msg.data
        out = parser.OFPPacketOut(
            datapath=datapath, buffer_id=msg.buffer_id,
            in_port=in_port, actions=actions, data=data
        )
        datapath.send_msg(out)

    # ------------------------------------------------------------------
    # Topology awareness
    # ------------------------------------------------------------------
    def _topo_discovery_loop(self):
        while True:
            hub.sleep(5)
            self._update_topology()

    def _update_topology(self):
        switches = get_switch(self)
        links = get_link(self)
        self.graph.clear()
        for s in switches:
            self.graph.add_node(s.dp.id)
        for l in links:
            src = l.src.dpid
            dst = l.dst.dpid
            self.graph.add_edge(src, dst, port=l.src.port_no)

    @set_ev_cls(topo_event.EventSwitchEnter)
    def switch_enter(self, ev):
        self._update_topology()

    @set_ev_cls(topo_event.EventLinkAdd)
    def link_add(self, ev):
        self._update_topology()
        self.logger.info("Link added – topology updated")

    @set_ev_cls(topo_event.EventLinkDelete)
    def link_delete(self, ev):
        self._update_topology()
        self.logger.info("Link deleted – topology updated, will recompute paths")
        # Clear sticky selections so next requests re-evaluate
        self.server_selection.clear()

    # ------------------------------------------------------------------
    # Server health / load (simplified: controller can mark servers down)
    # ------------------------------------------------------------------
    def mark_server_down(self, server_ip: str):
        if server_ip in SERVERS:
            SERVERS[server_ip]["alive"] = False
            # Clear selections that used this server
            to_del = [c for c, s in self.server_selection.items() if s == server_ip]
            for c in to_del:
                del self.server_selection[c]
            self.logger.warning("Server %s marked DOWN", server_ip)

    def mark_server_up(self, server_ip: str):
        if server_ip in SERVERS:
            SERVERS[server_ip]["alive"] = True
            self.logger.info("Server %s marked UP", server_ip)

    def set_server_load(self, server_ip: str, load: int):
        if server_ip in SERVERS:
            SERVERS[server_ip]["load"] = load

    # ------------------------------------------------------------------
    # Monitoring
    # ------------------------------------------------------------------
    def _monitor(self):
        while True:
            hub.sleep(10)
            alive = [f"{info['id']}({'UP' if info['alive'] else 'DOWN'},load={info['load']})"
                     for info in SERVERS.values()]
            self.logger.info("Servers: %s | Sticky maps: %d",
                             ", ".join(alive), len(self.server_selection))

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def state_change(self, ev):
        datapath = ev.datapath
        if ev.state == MAIN_DISPATCHER:
            self.datapaths[datapath.id] = datapath
        elif ev.state == DEAD_DISPATCHER:
            if datapath.id in self.datapaths:
                del self.datapaths[datapath.id]


# ------------------------------------------------------------------
# Simple REST API for demo / failure injection / status
# ------------------------------------------------------------------
class CDNRestController(ControllerBase):
    def __init__(self, req, link, data, **config):
        super(CDNRestController, self).__init__(req, link, data, **config)
        self.cdn_app = data["cdn_app"]

    @route("cdn", "/cdn/status", methods=["GET"])
    def get_status(self, req, **kwargs):
        body = {
            "servers": SERVERS,
            "selections": self.cdn_app.server_selection,
            "hosts": {k: list(v) for k, v in self.cdn_app.host_location.items()},
            "uptime": time.time() - self.cdn_app.start_time
        }
        return Response(content_type="application/json",
                        body=json.dumps(body, indent=2).encode("utf-8"))

    @route("cdn", "/cdn/server/{ip}/down", methods=["POST"])
    def server_down(self, req, ip, **kwargs):
        self.cdn_app.mark_server_down(ip)
        return Response(content_type="application/json",
                        body=json.dumps({"status": "ok", "server": ip, "alive": False}).encode())

    @route("cdn", "/cdn/server/{ip}/up", methods=["POST"])
    def server_up(self, req, ip, **kwargs):
        self.cdn_app.mark_server_up(ip)
        return Response(content_type="application/json",
                        body=json.dumps({"status": "ok", "server": ip, "alive": True}).encode())

    @route("cdn", "/cdn/server/{ip}/load/{load}", methods=["POST"])
    def set_load(self, req, ip, load, **kwargs):
        self.cdn_app.set_server_load(ip, int(load))
        return Response(content_type="application/json",
                        body=json.dumps({"status": "ok", "server": ip, "load": int(load)}).encode())
