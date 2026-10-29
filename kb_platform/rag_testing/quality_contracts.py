"""Offline architecture and I/O policy contracts for the project check command."""
from pathlib import Path
import tempfile
import unittest

from scripts.check_project import architecture_violations, deny_external_io


class ArchitectureContracts(unittest.TestCase):
    def test_agent_ports_reject_implementation_and_reverse_dependencies(self):
        for path, source in (
            ('agent_provider', 'from .remote import LunaClient'),
            ('agent_model', 'from .agent_luna import LunaModelProvider'),
            ('agent_model', 'import openai'),
            ('agent_plugins', 'from .knowledge_service import KnowledgeService'),
            ('agent_engine', 'from .knowledge_service import KnowledgeService'),
            ('agent_decisions', 'from .agent_model import AgentModel'),
            ('agent_tool_loop', 'from .knowledge_service import KnowledgeService'),
            ('plugin_native', 'from .core import Platform'),
            ('plugin_host', 'from .knowledge_service import KnowledgeService'),
            ('harness_loop', 'from .agent_workflow import KnowledgeWorkflow'),
            ('harness_services', 'from .agent_luna import LunaModelProvider'),
            ('harness_sessions', 'from .agent_store import AgentStore'),
            ('harness_loop', 'import openai'),
            ('plugin_host', 'import httpx'),
            ('harness_sessions', 'import psycopg'),
            ('agent_builtin_tools', 'from .agent_api import install_agent_routes'),
        ):
            with self.subTest(path=path, source=source):
                self.assertTrue(self.check_source(source, f'kb_platform/{path}.py'))
        self.assertEqual([], self.check_source('from .agent_provider import ModelProvider',
                                              'kb_platform/agent_model.py'))

    def test_harness_accepts_its_declared_generic_ports(self):
        for module, source in (
            ('plugin_native', 'from .agent_native import library_path'),
            ('plugin_host', 'from .plugin_native import native_call'),
            ('harness_loop', 'from .agent_engine import Runtime'),
            ('harness_loop', 'from .plugin_host import EventName'),
            ('harness_services', 'from .harness_sessions import MemorySession'),
            ('agent_tool_loop', 'from .harness_services import build_catalog'),
        ):
            with self.subTest(module=module):
                self.assertEqual([], self.check_source(source, f'kb_platform/{module}.py'))

    def check_native(self, manifest, *, crate='agent-harness', source='pub fn run() {}'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'kb_platform').mkdir()
            (root / 'kb_platform/service.py').write_text('value = 1', encoding='utf-8')
            native = root / 'rust' / crate
            (native / 'src').mkdir(parents=True)
            (native / 'Cargo.toml').write_text(manifest, encoding='utf-8')
            (native / 'src/lib.rs').write_text(source, encoding='utf-8')
            return architecture_violations(root)

    def test_native_crates_reject_business_dependencies_and_package_substitution(self):
        for manifest in (
            '[dependencies]\nknowledge-service = "1"',
            '[target.\'cfg(windows)\'.dependencies]\nknowledge-service = "1"',
            '[dependencies]\nserde = { package = "knowledge-service", version = "1" }',
            '[dependencies]\nserde = { path = "../../business" }',
            '[dependencies]\nserde = { workspace = true }',
            '[build-dependencies]\nknowledge-service = "1"',
            '[package]\nbuild = "generate.rs"',
            '[lib]\npath = "../../business.rs"',
            '[[bin]]\nname = "business"\npath = "../../business.rs"',
        ):
            with self.subTest(manifest=manifest):
                self.assertTrue(self.check_native(manifest))
        self.assertTrue(self.check_native(
            '[dependencies]\nagent-harness = { path = "../other-harness" }', crate='agent-kernel'))

    def test_native_crates_accept_reviewed_foundations_and_internal_sources(self):
        self.assertEqual([], self.check_native(
            '[dependencies]\nserde = { version = "1", features = ["derive"] }\nserde_json = "1"',
            source='#[path = "tests.rs"] mod tests;'))
        self.assertEqual([], self.check_native(
            '[dependencies]\nagent-harness = { path = "../agent-harness" }', crate='agent-kernel'))

    def test_native_sources_cannot_include_business_files_outside_the_crate(self):
        for source in (
            'include!("../../../business.rs");',
            'const CONFIG: &str = include_str!("../../../config/knowledge.json");',
            '#[path = "../../../business.rs"] mod business;',
            'include!(concat!(env!("OUT_DIR"), "/generated.rs"));',
        ):
            with self.subTest(source=source):
                self.assertTrue(self.check_native('[dependencies]\nserde = "1"', source=source))

    def check_source(self, source: str, path: str = "kb_platform/service.py") -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / path
            target.parent.mkdir(parents=True)
            target.write_text(source, encoding="utf-8")
            return architecture_violations(root)

    def test_product_rejects_execution_and_generated_dependencies(self):
        for source in (
            "import scripts.rag_test",
            "from scripts import rag_test",
            "from kb_platform.rag_testing import scorer",
            "from kb_platform.rag_testing.runner import run",
            "from .rag_testing.fixtures import sample",
            "import output.rag_evidence_testkit",
            "from reports import result",
            "from outputs import result",
        ):
            with self.subTest(source=source):
                self.assertTrue(self.check_source(source))

    def test_shared_contracts_are_valid_product_dependencies(self):
        for source in (
            "from kb_platform.rag_testing.budget import BudgetLedger",
            "from kb_platform.rag_testing.cache import StageCache",
            "from kb_platform.rag_testing.contracts import Trace",
            "from .rag_testing import contracts",
        ):
            with self.subTest(source=source):
                self.assertEqual([], self.check_source(source))

    def test_framework_may_use_its_own_fixtures(self):
        self.assertEqual([], self.check_source(
            "from .fixtures import sample", "kb_platform/rag_testing/example_contracts.py"
        ))

    def test_foundations_cannot_depend_on_product_or_scoring(self):
        for source in (
            "from kb_platform.service import execute",
            "from .scorer import score",
            "from .runner import run",
            "from .fixtures import sample",
            "from .provider_capture import observe_client",
            "from scripts.delivery_server_app import main",
        ):
            with self.subTest(source=source):
                self.assertTrue(self.check_source(source, "kb_platform/rag_testing/cache.py"))

    def test_foundations_may_share_contracts(self):
        self.assertEqual([], self.check_source(
            "from .contracts import Trace", "kb_platform/rag_testing/cache.py"
        ))

    def test_shared_capture_cannot_import_evaluation(self):
        for source in ("from .scoring import evaluate", "from scripts.rag_test import main",
                       "import sys\nsys.path.append('output')"):
            with self.subTest(source=source):
                self.assertTrue(self.check_source(source, "kb_platform/rag_testing/provider_capture.py"))

    def test_package_relative_imports_preserve_boundary(self):
        self.assertTrue(self.check_source(
            "from .rag_testing.scoring import evaluate", "kb_platform/__init__.py"
        ))

    def test_dynamic_imports_cannot_bypass_product_boundary(self):
        for source in (
            "import importlib\nimportlib.import_module('scripts.rag_test')",
            "__import__('kb_platform.rag_testing.scorer')",
            "import importlib\nimportlib.import_module(module_name)",
            "from importlib import import_module as load\nload('scripts.rag_test')",
            "import importlib\nimportlib.import_module('.scorer', 'kb_platform.rag_testing')",
        ):
            with self.subTest(source=source):
                self.assertTrue(self.check_source(source))

    def test_product_cannot_modify_import_search_path(self):
        for source in (
            "import sys\nsys.path.insert(0, 'output')",
            "import sys\nsys.path = ['output']",
            "import sys\nsys.path[:] = ['output']",
            "import sys as system\nsystem.path.append('output')",
            "from sys import path\npath.append('output')",
        ):
            with self.subTest(source=source):
                self.assertTrue(self.check_source(source))

    def test_reading_import_search_path_is_allowed(self):
        self.assertEqual([], self.check_source("import sys\nexisting = sys.path.copy()"))

    def test_empty_source_tree_does_not_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(architecture_violations(Path(directory)))

    def test_bare_contract_assertion_is_rejected(self):
        self.assertTrue(self.check_source(
            "def main():\n    assert True", "kb_platform/rag_testing/example_contracts.py"
        ))

    def test_parallel_test_tree_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "kb_platform").mkdir()
            (root / "kb_platform/service.py").write_text("value = 1", encoding="utf-8")
            (root / "tests").mkdir()
            self.assertTrue(architecture_violations(root))


class OfflinePolicyContracts(unittest.TestCase):
    def test_external_io_events_are_rejected(self):
        for event in (
            "socket.connect", "socket.getaddrinfo", "socket.sendto", "socket.sendmsg",
            "subprocess.Popen", "os.system", "os.posix_spawn", "os.exec"
        ):
            with self.subTest(event=event):
                with self.assertRaises(RuntimeError):
                    deny_external_io(event, ())

    def test_local_file_events_are_allowed(self):
        self.assertIsNone(deny_external_io("open", ("fixture.json", "r", 0)))
