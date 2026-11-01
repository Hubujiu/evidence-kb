import argparse
from pathlib import Path

from kb_platform.harness_services import builtin_plugins
from kb_platform.knowledge_profile import knowledge_plugins
from kb_platform.model_plugins import model_plugins
from kb_platform.platform_plugins import platform_plugins
from kb_platform.settings import Settings
from kb_platform.storage_cache_plugins import stage_cache_plugins
from kb_platform.storage_catalog import default_storage_providers
from kb_platform.storage_plugins import storage_plugins


def render():
    settings = Settings(Path('.local/store'), Path('.local/identities.json'))
    groups = (
        ('通用 Agent', 'harness_services.py', builtin_plugins()),
        ('公共模型服务', 'model_plugins.py', model_plugins()),
        ('平台基础设施', 'platform_plugins.py', platform_plugins(settings)),
        ('知识库问答', 'knowledge_profile.py', knowledge_plugins(None, None)),
        ('公共存储服务', 'storage_plugins.py', storage_plugins()),
        ('易失阶段缓存', 'storage_cache_plugins.py', stage_cache_plugins()),
    )
    count = sum(len(specs) for _, _, specs in groups)
    lines = ['# 当前插件目录', '',
        '本文件由 `uv run --no-sync python -m scripts.generate_plugin_catalog --output docs/plugin-catalog.md` 生成。', '',
        f'当前共有 {count} 个静态内置实现键。统计只构造 PluginSpec，不挂载宿主、不连接后端、不调用模型。', '',
        '模型提供商实例、存储实例、授权绑定和消费方包装按配置动态生成；外部安装的进程插件另计。公共服务可选，第三方可以自建服务和适配器。', '',
        '| 类别 | 数量 | 源码 |', '| --- | ---: | --- |']
    for group, module, specs in groups:
        lines.append(f'| {group} | {len(specs)} | [{module}](../kb_platform/{module}) |')
    lines.extend(['', '提供商声明的服务合同如下。它们是适配器类型，不额外增加静态 PluginSpec 计数。', '',
                  '| 提供商 | 服务出口及合同 |', '| --- | --- |'])
    for provider, entry in default_storage_providers().items():
        exports = '; '.join(name + ': ' + ', '.join(contract.key for contract in contracts)
                            for name, contracts in entry.exports.items())
        lines.append(f'| {provider} | {exports} |')
    lines.extend(['', '配置、显式初始化和迁移见 [存储服务](storage-plugins-design.md)。', '',
                  '| 类别 | 实现键 | 提供服务 | 静态依赖 | 存储槽位 |',
                  '| --- | --- | --- | --- | --- |'])
    for group, _, specs in groups:
        for spec in sorted(specs, key=lambda item: item.key):
            slots = ', '.join(slot.name + ': ' + slot.contract.key for slot in spec.storage_slots)
            lines.append(f'| {group} | `{spec.key}` | {", ".join(spec.provides) or "无"} | '
                         f'{", ".join(spec.requires) or "无"} | {slots or "无"} |')
    lines.extend(['', '消费者声明存储合同与可选预算槽位，生成的真实依赖由 Rust 宿主检查；装配层不补厂商约束。',
                  '合同 SDK 与注册器不强制第三方业务使用公共服务；模型和存储各自检查领域语义。', ''])
    return '\n'.join(lines), count


def main():
    parser = argparse.ArgumentParser(description='Generate the built-in plugin and storage service catalog without mounting.')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    document, count = render()
    args.output.write_text(document, encoding='utf-8')
    print(f'Generated {count} static plugin definitions')


if __name__ == '__main__':
    main()
