use crate::Error;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Manifest {
    pub id: String,
    pub version: String,
    pub provides: Vec<String>,
    pub requires: Vec<String>,
    #[serde(default)]
    pub overrides: Vec<String>,
    #[serde(default = "empty_config")]
    pub config: Value,
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
            || !valid_name(&plugin.version)
            || !plugin.config.is_object()
            || !unique_names(&plugin.provides)
            || !unique_names(&plugin.requires)
            || !unique_names(&plugin.overrides)
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
    let mut dependencies = vec![BTreeSet::new(); plugins.len()];
    for (index, plugin) in plugins.iter().enumerate() {
        for service in &plugin.requires {
            if let Some(provider) = providers.get(service) {
                dependencies[index].insert(*provider);
            } else if !inherited.contains(service) {
                return Err(Error::new(
                    "missing_service",
                    format!("{} requires {}", plugin.id, service),
                ));
            }
        }
    }
    let mut emitted = BTreeSet::new();
    let mut result = Vec::new();
    while emitted.len() < plugins.len() {
        let next = (0..plugins.len()).find(|index| {
            !emitted.contains(index) && dependencies[*index].iter().all(|dep| emitted.contains(dep))
        });
        let Some(next) = next else {
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
