"""Router-centered radial placement and obstacle-aware orthogonal link routing."""

import heapq
import math
from collections import deque
from dataclasses import dataclass


@dataclass
class Vertex:
    key: str
    width: int
    height: int
    kind: str = ""
    x: int = 0
    y: int = 0

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.width / 2, self.y + self.height / 2


@dataclass(frozen=True)
class Link:
    source: str
    target: str
    directed: bool = False


@dataclass
class Route:
    link: Link
    points: list[tuple[int, int]]
    source_port: tuple[float, float]
    target_port: tuple[float, float]


def radial_layout(vertices: dict[str, Vertex], links: list[Link]) -> tuple[int, int]:
    """Place router roots centrally and their graph neighborhoods in outer rings."""
    if not vertices:
        return 1000, 800
    adjacency = {key: set() for key in vertices}
    for link in links:
        adjacency[link.source].add(link.target)
        adjacency[link.target].add(link.source)
    roots = sorted(key for key, vertex in vertices.items() if vertex.kind == "router")
    if not roots:
        roots = sorted(key for key, vertex in vertices.items() if vertex.kind == "firewall")
    if not roots:
        roots = [min(vertices, key=lambda key: (-len(adjacency[key]), key))]
    depth = dict.fromkeys(roots, 0)
    parent = {}
    children = {key: [] for key in vertices}
    queue = deque(roots)
    while queue:
        source = queue.popleft()
        for target in sorted(adjacency[source]):
            if target not in depth:
                depth[target] = depth[source] + 1
                parent[target] = source
                children[source].append(target)
                queue.append(target)
    # Unconnected inventory groups remain visible without inventing links.
    detached = sorted(set(vertices) - depth.keys())
    for key in detached:
        depth[key] = 1
    radius = {key: math.hypot(vertex.width, vertex.height) / 2 + 60
              for key, vertex in vertices.items()}

    def weight(key: str) -> float:
        return max(radius[key] * 2, sum(weight(child) for child in children[key]))

    angles = {}

    def sector(key: str, start: float, span: float) -> None:
        angles[key] = start + span / 2
        total = sum(weight(child) for child in children[key])
        for child in children[key]:
            child_span = span * weight(child) / total
            sector(child, start, child_span)
            start += child_span

    branches = [child for root in roots for child in children[root]] + detached
    # External/upstream infrastructure is placed at the top of the radial view.
    branches.sort(key=lambda key: (vertices[key].kind not in ("firewall", "cloud"), key))
    total = sum(weight(key) for key in branches)
    start = -math.pi / 2 - (math.pi * weight(branches[0]) / total if branches else 0)
    for key in branches:
        span = 2 * math.pi * weight(key) / total
        sector(key, start, span)
        start += span

    core_radius = 0 if len(roots) == 1 else max(radius[key] for key in roots) / math.sin(math.pi / len(roots))
    for index, key in enumerate(roots):
        angles[key] = -math.pi / 2 + 2 * math.pi * index / len(roots)
    centers = {key: (core_radius * math.cos(angles[key]), core_radius * math.sin(angles[key])) for key in roots}
    distances = dict.fromkeys(roots, core_radius)
    # Grow each branch independently: a large VLAN must not push unrelated
    # firewall/switch/server chains onto an unnecessarily enormous ring.
    for key in sorted(set(vertices) - set(roots), key=lambda key: (depth[key], vertices[key].kind == "group", key)):
        ancestor = parent.get(key)
        distance = ((distances[ancestor] + radius[ancestor]) if ancestor else core_radius + max(radius[root] for root in roots)) + radius[key] + 120
        ux, uy = math.cos(angles[key]), math.sin(angles[key])
        # Move outward past every forbidden circle interval along this ray.
        intervals = []
        for other, (ox, oy) in centers.items():
            projection = ox * ux + oy * uy
            perpendicular = max(0, ox * ox + oy * oy - projection * projection)
            combined = radius[key] + radius[other]
            if perpendicular < combined * combined:
                extent = math.sqrt(combined * combined - perpendicular)
                intervals.append((projection - extent, projection + extent))
        for start, end in sorted(intervals):
            if start <= distance <= end:
                distance = end + 1
        distances[key] = distance
        centers[key] = distance * ux, distance * uy
    for key, vertex in vertices.items():
        x, y = centers[key]
        vertex.x = round(x - vertex.width / 2)
        vertex.y = round(y - vertex.height / 2)
    # Symmetric canvas bounds keep the router core at the true map center.
    half_width = max(max(abs(vertex.x), abs(vertex.x + vertex.width)) for vertex in vertices.values()) + 180
    half_height = max(max(abs(vertex.y), abs(vertex.y + vertex.height)) for vertex in vertices.values()) + 180
    for vertex in vertices.values():
        vertex.x += half_width
        vertex.y += half_height
    return half_width * 2, half_height * 2


def _intersects(a: tuple[int, int], b: tuple[int, int], rectangle: tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = rectangle
    if a[0] == b[0]:
        return left < a[0] < right and max(min(a[1], b[1]), top) < min(max(a[1], b[1]), bottom)
    return top < a[1] < bottom and max(min(a[0], b[0]), left) < min(max(a[0], b[0]), right)


def route_links(vertices: dict[str, Vertex], links: list[Link], width: int, height: int) -> list[Route]:
    """Reserve unique ports and tracks; never share positive-length line segments.

    Crossings are penalized heavily, but permitted for non-planar graphs. The
    renderer shows bridges at these crossings rather than ambiguous junctions.
    """
    if not links:
        return []
    attachments: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for index, link in enumerate(links):
        for endpoint, other in ((link.source, link.target), (link.target, link.source)):
            x, y = vertices[endpoint].center
            ox, oy = vertices[other].center
            side = ("right" if ox > x else "left") if abs(ox - x) > abs(oy - y) else ("bottom" if oy > y else "top")
            attachments.setdefault((endpoint, side), []).append((index, other))
    ports = {}
    for (key, side), entries in attachments.items():
        vertex = vertices[key]
        horizontal = side in ("top", "bottom")
        entries.sort(key=lambda item: (vertices[item[1]].center[0 if horizontal else 1], item[1]))
        length = vertex.width if horizontal else vertex.height
        for number, (index, _) in enumerate(entries, 1):
            offset = round(16 + (length - 32) * number / (len(entries) + 1))
            if side == "top":
                point, escape = (vertex.x + offset, vertex.y), (vertex.x + offset, vertex.y - 32)
            elif side == "bottom":
                point, escape = (vertex.x + offset, vertex.y + vertex.height), (vertex.x + offset, vertex.y + vertex.height + 32)
            elif side == "left":
                point, escape = (vertex.x, vertex.y + offset), (vertex.x - 32, vertex.y + offset)
            else:
                point, escape = (vertex.x + vertex.width, vertex.y + offset), (vertex.x + vertex.width + 32, vertex.y + offset)
            ports[index, key] = (point, escape)
    rectangles = [(vertex.x - 12, vertex.y - 12, vertex.x + vertex.width + 12, vertex.y + vertex.height + 12)
                  for vertex in vertices.values()]
    # All attachment stubs are reserved before routing any link.
    stubs = [(min(point[0], escape[0]) - 2, min(point[1], escape[1]) - 2,
              max(point[0], escape[0]) + 2, max(point[1], escape[1]) + 2)
             for point, escape in ports.values()]
    xs, ys = {60, width - 60}, {130, height - 60}
    for vertex in vertices.values():
        for margin in (32, 56, 80):
            xs.update((vertex.x - margin, vertex.x + vertex.width + margin))
            ys.update((vertex.y - margin, vertex.y + vertex.height + margin))
        xs.add(vertex.x + vertex.width // 2)
        ys.add(vertex.y + vertex.height // 2)
    for point, escape in ports.values():
        xs.update((point[0], escape[0]))
        ys.update((point[1], escape[1]))
    # Keep every route below the title/legend and inside the canvas margins.
    xs = sorted(value for value in xs if 60 <= value <= width - 60)
    ys = sorted(value for value in ys if 130 <= value <= height - 60)
    x_index, y_index = {value: index for index, value in enumerate(xs)}, {value: index for index, value in enumerate(ys)}
    occupied = set()
    used_axes: dict[tuple[int, int], int] = {}
    blocked_cache = {}
    tracks = []
    routes = []
    # Short local links first; long shared-VLAN links can use the outer lanes.
    order = sorted(range(len(links)), key=lambda index: (
        math.dist(vertices[links[index].source].center, vertices[links[index].target].center), index))
    for index in order:
        link = links[index]
        source_point, source_escape = ports[index, link.source]
        target_point, target_escape = ports[index, link.target]
        start = (x_index[source_escape[0]], y_index[source_escape[1]])
        goal = (x_index[target_escape[0]], y_index[target_escape[1]])
        source_axis = int(source_point[0] == source_escape[0])
        target_axis = int(target_point[0] == target_escape[0])
        own_stubs = {
            (min(point[0], escape[0]) - 2, min(point[1], escape[1]) - 2,
             max(point[0], escape[0]) + 2, max(point[1], escape[1]) + 2)
            for point, escape in ((source_point, source_escape), (target_point, target_escape))}

        def heuristic(node: tuple[int, int]) -> int:
            return abs(xs[node[0]] - target_escape[0]) + abs(ys[node[1]] - target_escape[1])

        initial = (*start, source_axis)
        queue = [(heuristic(start), 0, initial)]
        costs = {initial: 0}
        parents = {}
        track_cache = {}
        found = None
        while queue:
            _, cost, state = heapq.heappop(queue)
            if cost != costs[state]:
                continue
            ix, iy, previous_axis = state
            here = (ix, iy)
            if here == goal:
                found = state
                break
            for dx, dy, axis in ((-1, 0, 0), (1, 0, 0), (0, -1, 1), (0, 1, 1)):
                next_node = (ix + dx, iy + dy)
                nx, ny = next_node
                if not (0 <= nx < len(xs) and 0 <= ny < len(ys)):
                    continue
                edge = tuple(sorted((here, next_node)))
                if edge in occupied:
                    continue
                if edge not in track_cache:
                    a, b = (xs[ix], ys[iy]), (xs[nx], ys[ny])
                    track_cache[edge] = any(
                        (a[1] == b[1] and c[1] == d[1] and abs(a[1] - c[1]) < 8
                         and max(min(a[0], b[0]), min(c[0], d[0])) < min(max(a[0], b[0]), max(c[0], d[0])))
                        or (a[0] == b[0] and c[0] == d[0] and abs(a[0] - c[0]) < 8
                            and max(min(a[1], b[1]), min(c[1], d[1])) < min(max(a[1], b[1]), max(c[1], d[1])))
                        for c, d in tracks)
                if track_cache[edge]:
                    continue
                mask = used_axes.get(here, 0)
                next_mask = used_axes.get(next_node, 0)
                if mask and (mask == 3 or mask & (1 << axis) or axis != previous_axis):
                    continue
                if next_mask and (next_mask == 3 or next_mask & (1 << axis)):
                    continue
                if edge not in blocked_cache:
                    a, b = (xs[ix], ys[iy]), (xs[nx], ys[ny])
                    blocked_cache[edge] = (any(_intersects(a, b, rectangle) for rectangle in rectangles),
                                           [stub for stub in stubs if _intersects(a, b, stub)])
                blocked, hits = blocked_cache[edge]
                if blocked or any(stub not in own_stubs for stub in hits):
                    continue
                length = abs(xs[nx] - xs[ix]) + abs(ys[ny] - ys[iy])
                new_cost = cost + length + (60 if axis != previous_axis else 0) + (2000 if next_mask else 0)
                if next_node == goal and axis != target_axis:
                    new_cost += 60
                next_state = (*next_node, axis)
                if new_cost < costs.get(next_state, float("inf")):
                    costs[next_state] = new_cost
                    parents[next_state] = state
                    heapq.heappush(queue, (new_cost + heuristic(next_node), new_cost, next_state))
        if found is None:
            raise ValueError(f"Cannot route connection without overlapping lines: {link.source} -> {link.target}")
        path = []
        while found != initial:
            path.append(found[:2])
            found = parents[found]
        path.append(start)
        path.reverse()
        for a, b in zip(path, path[1:]):
            occupied.add(tuple(sorted((a, b))))
            bit = 1 if a[1] == b[1] else 2
            used_axes[a] = used_axes.get(a, 0) | bit
            used_axes[b] = used_axes.get(b, 0) | bit
        points = [source_point] + [(xs[ix], ys[iy]) for ix, iy in path] + [target_point]
        simplified = []
        for point in points:
            if simplified and point == simplified[-1]:
                continue
            while len(simplified) >= 2 and (
                    simplified[-2][0] == simplified[-1][0] == point[0]
                    or simplified[-2][1] == simplified[-1][1] == point[1]):
                simplified.pop()
            simplified.append(point)
        tracks.extend(zip(simplified, simplified[1:]))
        source, target = vertices[link.source], vertices[link.target]
        routes.append(Route(link, simplified,
                            ((source_point[0] - source.x) / source.width, (source_point[1] - source.y) / source.height),
                            ((target_point[0] - target.x) / target.width, (target_point[1] - target.y) / target.height)))
    return routes
