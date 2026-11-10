import ast


def formal_tool_isolation_violations(source):
    errors = []
    try:
        tree = ast.parse(source)
        for name, owner in (('run_model', "directory = output / spec['id']"),
                            ('run_inline_export', 'directory = output / identity')):
            functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
            if len(functions) != 1:
                errors.append(f'formal-tool: {name} must own exactly one execution entrypoint')
                continue
            function = functions[0]
            expected = ast.parse(owner + "\ndirectory.mkdir()\nscratch = (directory / 'java-tmp').resolve()\nscratch.mkdir()")
            declarations = [ast.dump(node, include_attributes=False) for node in expected.body]
            statements = [ast.dump(node, include_attributes=False) for node in function.body]
            starts = [i for i in range(len(statements)) if statements[i:i + 4] == declarations]
            if len(starts) != 1:
                errors.append(f'formal-tool: {name} requires exclusive per-run absolute Java scratch before launch')
            assignments = [node for node in ast.walk(function) if isinstance(node, ast.Assign)
                           and any(isinstance(target, ast.Name) and target.id == 'command' for target in node.targets)]
            flag = ast.parse("f'-Djava.io.tmpdir={scratch}'", mode='eval').body
            if len(assignments) != 1 or not isinstance(assignments[0].value, ast.List):
                errors.append(f'formal-tool: {name} requires one explicit Java command')
                continue
            command = assignments[0].value.elts
            flags = [i for i, node in enumerate(command)
                     if '-Djava.io.tmpdir' in ast.unparse(node)]
            classpath = [i for i, node in enumerate(command) if isinstance(node, ast.Constant) and node.value == '-cp']
            if (len(flags) != 1 or ast.dump(command[flags[0]]) != ast.dump(flag)
                    or len(classpath) != 1 or flags[0] >= classpath[0]
                    or not starts or assignments[0].lineno <= function.body[starts[0] + 3].lineno):
                errors.append(f'formal-tool: {name} must pass its own absolute scratch as a JVM option')
            writes = [node for node in ast.walk(function) if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                      and any(isinstance(target, ast.Name) and target.id == 'scratch'
                              for target in (node.targets if isinstance(node, ast.Assign) else [node.target]))]
            if len(writes) != 1:
                errors.append(f'formal-tool: {name} cannot replace its exclusive scratch directory')
            calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)
                     and ast.unparse(node.func) == 'subprocess.run']
            if len(calls) != 1 or not calls[0].args or ast.unparse(calls[0].args[0]) != 'command':
                errors.append(f'formal-tool: {name} must launch the isolated command once')
    except (SyntaxError, ValueError, TypeError, IndexError):
        errors.append('formal-tool: Java execution ownership is malformed')
    return errors
