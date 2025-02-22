import os
import json
import socket
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from prometheus_client import Gauge, start_http_server as start_prometheus_server
from threading import Thread
from time import sleep
from urllib.parse import parse_qs, urlparse


nodes_gauge = Gauge("kube_nettopo_nodes", "Registered nodes", ["node", "ip"])
edges_gauge = Gauge("kube_nettopo_edges", "Network edges between nodes", ["source", "target", "type", "interface"])

def run_command(cmd):
    """Запускает команду и возвращает её вывод в JSON или как строку."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return result.stdout.strip()

def get_node_name():
    """Получает имя ноды из окружения Kubernetes или hostname."""
    return os.getenv("NODE_NAME", socket.gethostname())

def get_ip_addr():
    """Собирает данные об интерфейсах."""
    return run_command(["ip", "-j", "addr"])

def get_routes():
    """Собирает таблицу маршрутизации."""
    return run_command(["ip", "-j", "route"])

def get_neighbors():
    """Собирает ARP-таблицу (соседей)."""
    return run_command(["ip", "-j", "neigh"])

def analyze_topology(ip_data, routes, neighbors):
    """Анализирует топологию на основе сетевых данных."""
    node_name = get_node_name()
    topology = {"node": node_name, "ips": [], "links": []}

    for iface in ip_data:
        for addr in iface.get("addr_info", []):
            if addr["family"] == "inet":  # IPv4
                topology["ips"].append(addr["local"])


    for neigh in neighbors:
        if isinstance(neigh, dict) and neigh.get("dst"):
            topology["links"].append({
                "to": neigh["dst"],
                "type": "L2_neighbor",
                "interface": neigh.get("dev", "unknown")
            })

    for route in routes:
        if isinstance(route, dict) and route.get("gateway"):
            gw = route["gateway"]
            if gw not in topology["ips"]: 
                topology["links"].append({
                    "to": gw,
                    "type": "L3_gateway",
                    "interface": route.get("dev", "unknown")
                })

    return topology

def export_to_prometheus(topology):
    """Экспортирует топологию в метрики Prometheus."""
    for ip in topology["ips"]:
        nodes_gauge.labels(node=topology["node"], ip=ip).set(1)
    for link in topology["links"]:
        edges_gauge.labels(
            source=topology["node"],
            target=link["to"],
            type=link["type"],
            interface=link["interface"]
        ).set(1)


class NodeGraphHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed_path = urlparse(self.path)
        path = parsed_path.path 
        query_params = parse_qs(parsed_path.query)

        if path == "/api/health":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok"}).encode())
        elif path == "/api/graph/fields":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            nodes_fields = [
                {"field_name": "id", "type": "string"},
                {"field_name": "title", "type": "string"},
                {"field_name": "subTitle", "type": "string"},
                {"field_name": "detail__role", "type": "string", "displayName": "Role"},
                {"field_name": "arc__failed", "type": "number", "color": "red", "displayName": "Failed"},
                {"field_name": "arc__passed", "type": "number", "color": "green", "displayName": "Passed"},
                {"field_name": "mainStat", "type": "string"}
            ]
            edges_fields = [
                {"field_name": "id", "type": "string"},
                {"field_name": "source", "type": "string"},
                {"field_name": "target", "type": "string"},
                {"field_name": "mainStat", "type": "number"}
            ]
            result = {"nodes_fields": nodes_fields, "edges_fields": edges_fields}
            self.wfile.write(json.dumps(result).encode())
        elif path == "/api/graph/data":
            topology = collect_topology()
            data = format_for_nodegraph(topology, query_params)
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        else:
            self.send_error(404)

def collect_topology():
    """Собирает текущую топологию сети."""
    ip_data = get_ip_addr()
    routes = get_routes()
    neighbors = get_neighbors()
    return analyze_topology(ip_data, routes, neighbors)

def format_for_nodegraph(topology, query_params):

    nodes = [
        {
            "id": f"node-{i+1}", #(1, 2, ...)
            "title": topology["node"],
            "subTitle": ip,
            "detail__role": "network_node", 
            "arc__failed": 0.0, 
            "arc__passed": 1.0, 
            "mainStat": "active"
        }
        for i, ip in enumerate(topology["ips"]) if topology["ips"] and topology["node"]
    ]
    edges = [
        {
            "id": f"edge-{i}", 
            "source": topology["node"],
            "target": link["to"],
            "mainStat": 100 
        }
        for i, link in enumerate(topology["links"]) if topology["links"] and topology["node"]
    ]


    if "kube_nettopo_nodes" in query_params or "nodes" in query_params:
        return {"nodes": nodes, "edges": []}
    elif "kube_nettopo_edges" in query_params or "edges" in query_params:
        return {"nodes": [], "edges": edges}
    elif "query" in query_params:
        query = query_params.get("query", [None])[0]
        if query == "text1":
            nodes = [n for n in nodes if n['title'] == topology["node"]]  
            edges = [e for e in edges if e['mainStat'] > 50]
        return {"nodes": nodes, "edges": edges}
    elif "service" in query_params:
        service = query_params.get("service", [None])[0]
        if service == "processors":
            nodes = [n for n in nodes if n['id'].startswith("processor")]
            edges = [e for e in edges if e['source'].startswith("processor")]
        return {"nodes": nodes, "edges": edges}
    else:
        return {"nodes": nodes, "edges": edges}

def topology_updater():
    """Фоновая функция для периодического обновления топологии и метрик."""
    while True:
        topology = collect_topology()
        export_to_prometheus(topology)
        sleep(60)

def start_servers():

    start_prometheus_server(8000)
    print("Starting Prometheus server on port 8000...")
    
    server = HTTPServer(("", 8001), NodeGraphHandler)
    print("Starting Node Graph API server on port 8001...")
    
    server.serve_forever()

def main():
    topology_thread = Thread(target=topology_updater, daemon=True)
    topology_thread.start()
    
    start_servers()

if __name__ == "__main__":
    main()