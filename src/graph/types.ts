export type NodeType =
	| "monument"
	| "component"
	| "person"
	| "material"
	| "style"
	| "inspiration"
	| "event";

export interface GraphNode {
	id: string;
	name: string;
	type: NodeType;
	aliases?: string[];
	location?: string;
}

export interface GraphEdge {
	subject: string;
	relation: string;
	object: string;
	source_doc?: string;
	year?: number | string;
}

export interface GraphData {
	nodes: GraphNode[];
	edges: GraphEdge[];
}
