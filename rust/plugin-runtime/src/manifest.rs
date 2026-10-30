use crate::Error;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub id: String,
    #[serde(default)]
    pub implementation: String,
    pub version: String,
    pub provides: Vec<String>,
    pub requires: Vec<String>,
    #[serde(default)]
    pub overrides: Vec<String>,
    #[serde(default = "empty_config")]
    pub config: Value,
    #[serde(default = "serial")]
    pub concurrency: String,
    #[serde(default)]
    pub configuration_inputs: BTreeMap<String, String>,
}

fn serial() -> String {
    "serial".into()
}
fn empty_config() -> Value {
    serde_json::json!({})
}

pub fn valid_name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._:/-".contains(&b))
}

fn unique_names(values: &[String]) -> bool {
    values.len() <= 256
        && values.iter().all(|v| valid_name(v))
        && values.iter().collect::<BTreeSet<_>>().len() == values.len()
}

pub fn order(plugins: &[Manifest], inherited: &BTreeSet<String>) -> Result<Vec<String>, Error> {
    if plugins.len() > 256 {
        return Err(Error::new("manifest_limit", "too many plugins"));
    }
    let mut identifiers = BTreeSet::new();
    let mut providers = BTreeMap::new();
    for (index, plugin) in plugins.iter().enumerate() {
        if !valid_name(&plugin.id)
            || (!plugin.implementation.is_empty() && !valid_name(&plugin.implementation))
            || !valid_name(&plugin.version)
            || !plugin.config.is_object()
            || plugin.configuration_inputs.len() > 256
            || !plugin.configuration_inputs.iter().all(|(key, digest)| {
                !key.is_empty()
                    && key.len() <= 4096
                    && !key.chars().any(char::is_control)
                    && digest.len() == 64
                    && digest.bytes().all(|byte| byte.is_ascii_hexdigit())
            })
            || !unique_names(&plugin.provides)
            || !unique_names(&plugin.requires)
            || !unique_names(&plugin.overrides)
            || !matches!(plugin.concurrency.as_str(), "serial" | "concurrent")
        {
            return Err(Error::new(
                "invalid_manifest",
                format!("invalid manifest for {}", plugin.id),
            ));
        }
        if !identifiers.insert(&plugin.id) {
            return Err(Error::new(
                "duplicate_plugin",
                format!("duplicate plugin {}", plugin.id),
            ));
        }
        for service in &plugin.overrides {
            if !plugin.provides.contains(service) || !inherited.contains(service) {
                return Err(Error::new(
                    "invalid_override",
                    format!("{} is not an inherited provided service", service),
                ));
            }
        }
        for service in &plugin.provides {
            if providers.insert(service, index).is_some() {
                return Err(Error::new(
                    "duplicate_service",
                    format!("duplicate service {}", service),
                ));
            }
            if inherited.contains(service) && !plugin.overrides.contains(service) {
                return Err(Error::new(
                    "implicit_override",
                    format!("service {} requires an explicit override", service),
                ));
            }
        }
    }
    let dependencies: Vec<BTreeSet<usize>> = plugins
        .iter()
        .map(|plugin| {
            plugin
                .requires
                .iter()
                .filter_map(|service| providers.get(service).copied())
                .collect()
        })
        .collect();
    let mut emitted = BTreeSet::new();
    let mut result = Vec::new();
    while emitted.len() < plugins.len() {
        let Some(next) = (0..plugins.len()).find(|index| {
            !emitted.contains(index) && dependencies[*index].iter().all(|dep| emitted.contains(dep))
        }) else {
            return Err(Error::new(
                "dependency_cycle",
                "plugin service dependency cycle",
            ));
        };
        emitted.insert(next);
        result.push(plugins[next].id.clone());
    }
    Ok(result)
}
