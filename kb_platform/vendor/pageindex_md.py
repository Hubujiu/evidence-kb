# MIT: VectifyAI/PageIndex b81465d9ad3d80ee1df344508a85d1f2cdfb8a26
# Three unchanged pure Markdown indexing functions; see pageindex-LICENSE.
import re

def extract_nodes_from_markdown(markdown_content):
    header_pattern = r'^(#{1,6})\s+(.+)$'
    bold_heading_pattern = r'^\*\*(.+?)\*\*\s*$'
    code_block_pattern = r'^```'
    node_list = []
    
    lines = markdown_content.split('\n')
    in_code_block = False
    
    for line_num, line in enumerate(lines, 1):
        stripped_line = line.strip()
        
        # Check for code block delimiters (triple backticks)
        if re.match(code_block_pattern, stripped_line):
            in_code_block = not in_code_block
            continue
        
        # Skip empty lines
        if not stripped_line:
            continue
        
        # Only look for headers when not inside a code block
        if not in_code_block:
            match = re.match(header_pattern, stripped_line)
            if match:
                title = match.group(2).strip()
                level = len(match.group(1))
                node_list.append({'node_title': title, 'line_num': line_num, 'level': level})
                continue

            bold_match = re.match(bold_heading_pattern, stripped_line)
            if bold_match:
                title = bold_match.group(1).strip()
                if title:
                    node_list.append({'node_title': title, 'line_num': line_num, 'level': 1})

    return node_list, lines

def extract_node_text_content(node_list, markdown_lines):    
    all_nodes = []
    for node in node_list:
        processed_node = {
            'title': node['node_title'],
            'line_num': node['line_num'],
            'level': node['level']
        }
        all_nodes.append(processed_node)
    
    for i, node in enumerate(all_nodes):
        start_line = node['line_num'] - 1 
        if i + 1 < len(all_nodes):
            end_line = all_nodes[i + 1]['line_num'] - 1 
        else:
            end_line = len(markdown_lines)
        
        node['text'] = '\n'.join(markdown_lines[start_line:end_line]).strip()    
    return all_nodes

def build_tree_from_nodes(node_list):
    if not node_list:
        return []
    
    stack = []
    root_nodes = []
    node_counter = 1
    
    for node in node_list:
        current_level = node['level']
        
        tree_node = {
            'title': node['title'],
            'node_id': str(node_counter).zfill(4),
            'text': node['text'],
            'line_num': node['line_num'],
            'nodes': []
        }
        node_counter += 1
        
        while stack and stack[-1][1] >= current_level:
            stack.pop()
        
        if not stack:
            root_nodes.append(tree_node)
        else:
            parent_node, parent_level = stack[-1]
            parent_node['nodes'].append(tree_node)
        
        stack.append((tree_node, current_level))
    
    return root_nodes
