import * as d3 from "d3";
import type { GraphData, GraphEdge, GraphNode, NodeType } from "./types";

export interface ForceGraphOptions {
	width?: number;
	height?: number;
	/** Node id to focus the highlighted neighborhood on when the graph first renders. */
	initialFocus?: string;
}

export interface ForceGraphHandle {
	destroy(): void;
	focus(nodeId: string | null): void;
}

const NODE_TYPE_COLORS: Record<NodeType, string> = {
	monument: "#e07a5f",
	component: "#f2cc8f",
	person: "#3d5a80",
	material: "#9c89b8",
	style: "#e63946",
	inspiration: "#457b9d",
	event: "#6d6875",
};

type SimNode = GraphNode & d3.SimulationNodeDatum;
type SimEdge = d3.SimulationLinkDatum<SimNode> & Omit<GraphEdge, "subject" | "object">;

/**
 * Renders an interactive force-directed view of a knowledge graph into `container`.
 * Framework-agnostic (no React/Svelte/etc. dependency) so it can be mounted from
 * a plain <script>, an Astro client:only island, or any other host.
 */
export function renderKnowledgeGraph(container: HTMLElement, data: GraphData, options: ForceGraphOptions = {}): ForceGraphHandle {
	const width = options.width ?? (container.clientWidth || 800);
	const height = options.height ?? (container.clientHeight || 600);

	const nodesById = new Map<string, SimNode>(data.nodes.map((n) => [n.id, { ...n }]));
	const nodes: SimNode[] = [...nodesById.values()];
	const links: SimEdge[] = data.edges.map((e) => ({
		...e,
		source: e.subject,
		target: e.object,
	}));

	const neighbors = new Map<string, Set<string>>();
	for (const node of nodes) neighbors.set(node.id, new Set([node.id]));
	for (const edge of data.edges) {
		neighbors.get(edge.subject)?.add(edge.object);
		neighbors.get(edge.object)?.add(edge.subject);
	}

	container.innerHTML = "";

	const root = d3.select(container).append("div").attr("class", "g2rag-graph").style("position", "relative");

	const svg = root
		.append("svg")
		.attr("width", "100%")
		.attr("height", "100%")
		.attr("viewBox", `0 0 ${width} ${height}`)
		.attr("font-family", "system-ui, sans-serif");

	const tooltip = root
		.append("div")
		.attr("class", "g2rag-tooltip")
		.style("position", "absolute")
		.style("pointer-events", "none")
		.style("opacity", 0)
		.style("background", "rgba(20, 20, 24, 0.92)")
		.style("color", "#fff")
		.style("padding", "6px 10px")
		.style("border-radius", "6px")
		.style("font-size", "12px")
		.style("max-width", "260px")
		.style("z-index", "10");

	const zoomLayer = svg.append("g");

	svg.call(
		d3
			.zoom<SVGSVGElement, unknown>()
			.scaleExtent([0.2, 4])
			.on("zoom", (event) => zoomLayer.attr("transform", event.transform))
	);

	const link = zoomLayer
		.append("g")
		.attr("stroke", "#94a3b8")
		.attr("stroke-opacity", 0.5)
		.selectAll("line")
		.data(links)
		.join("line")
		.attr("stroke-width", 1.4);

	const node = zoomLayer
		.append("g")
		.selectAll<SVGCircleElement, SimNode>("circle")
		.data(nodes)
		.join("circle")
		.attr("r", (d) => (d.type === "monument" ? 12 : d.type === "component" ? 9 : 7))
		.attr("fill", (d) => NODE_TYPE_COLORS[d.type])
		.attr("stroke", "#fff")
		.attr("stroke-width", 1.5)
		.style("cursor", "pointer");

	const label = zoomLayer
		.append("g")
		.selectAll<SVGTextElement, SimNode>("text")
		.data(nodes)
		.join("text")
		.text((d) => d.name)
		.attr("font-size", 10)
		.attr("dx", 10)
		.attr("dy", 4)
		.attr("fill", "#1f2937")
		.style("pointer-events", "none");

	const simulation = d3
		.forceSimulation(nodes)
		.force(
			"link",
			d3
				.forceLink<SimNode, SimEdge>(links)
				.id((d) => d.id)
				.distance(70)
				.strength(0.5)
		)
		.force("charge", d3.forceManyBody().strength(-220))
		.force("center", d3.forceCenter(width / 2, height / 2))
		.force("collide", d3.forceCollide(24));

	simulation.on("tick", () => {
		link
			.attr("x1", (d) => (d.source as SimNode).x ?? 0)
			.attr("y1", (d) => (d.source as SimNode).y ?? 0)
			.attr("x2", (d) => (d.target as SimNode).x ?? 0)
			.attr("y2", (d) => (d.target as SimNode).y ?? 0);
		node.attr("cx", (d) => d.x ?? 0).attr("cy", (d) => d.y ?? 0);
		label.attr("x", (d) => d.x ?? 0).attr("y", (d) => d.y ?? 0);
	});

	node.call(
		d3
			.drag<SVGCircleElement, SimNode>()
			.on("start", (event, d) => {
				if (!event.active) simulation.alphaTarget(0.3).restart();
				d.fx = d.x;
				d.fy = d.y;
			})
			.on("drag", (event, d) => {
				d.fx = event.x;
				d.fy = event.y;
			})
			.on("end", (event, d) => {
				if (!event.active) simulation.alphaTarget(0);
				d.fx = null;
				d.fy = null;
			})
	);

	function focus(nodeId: string | null) {
		const activeSet = nodeId ? neighbors.get(nodeId) : null;
		node
			.attr("opacity", (d) => (!activeSet || activeSet.has(d.id) ? 1 : 0.15))
			.attr("stroke", (d) => (nodeId && d.id === nodeId ? "#111827" : "#fff"))
			.attr("stroke-width", (d) => (nodeId && d.id === nodeId ? 3 : 1.5));
		label.attr("opacity", (d) => (!activeSet || activeSet.has(d.id) ? 1 : 0.15));
		link.attr("opacity", (d) => {
			if (!activeSet) return 0.5;
			const s = (d.source as SimNode).id ?? d.source;
			const t = (d.target as SimNode).id ?? d.target;
			return s === nodeId || t === nodeId ? 0.9 : 0.05;
		});
	}

	node
		.on("click", (_event, d) => {
			focus(d.id === focusedId ? null : d.id);
			focusedId = d.id === focusedId ? null : d.id;
		})
		.on("mouseenter", (event, d) => {
			const edgesForNode = data.edges.filter((e) => e.subject === d.id || e.object === d.id);
			const facts = edgesForNode
				.slice(0, 6)
				.map((e) => `${nodesById.get(e.subject)?.name ?? e.subject} — ${e.relation.replaceAll("_", " ")} → ${nodesById.get(e.object)?.name ?? e.object}`)
				.join("<br/>");
			const locationLine = d.location ? `<br/><em>${d.location}</em>` : "";
			tooltip
				.style("opacity", 1)
				.html(`<strong>${d.name}</strong> <span style="opacity:.7">(${d.type})</span>${locationLine}<br/>${facts}`)
				.style("left", `${event.offsetX + 14}px`)
				.style("top", `${event.offsetY + 14}px`);
		})
		.on("mousemove", (event) => {
			tooltip.style("left", `${event.offsetX + 14}px`).style("top", `${event.offsetY + 14}px`);
		})
		.on("mouseleave", () => tooltip.style("opacity", 0));

	let focusedId: string | null = options.initialFocus ?? null;
	if (focusedId) focus(focusedId);

	return {
		destroy() {
			simulation.stop();
			container.innerHTML = "";
		},
		focus(nodeId: string | null) {
			focusedId = nodeId;
			focus(nodeId);
		},
	};
}

export { NODE_TYPE_COLORS };
export type { GraphData, GraphEdge, GraphNode, NodeType };
