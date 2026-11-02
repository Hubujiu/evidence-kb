use crate::{Error, Manifest, manifest};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::{BTreeMap, BTreeSet};

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Contract {
    pub id: String,
    pub major: u32,
    #[serde(default)]
    pub capabilities: BTreeSet<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Requirement {
    pub contract: Contract,
    #[serde(default)]
    pub optional: bool,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Definition {
    pub implementation: String,
    pub version: String,
    pub command: Vec<String>,
    pub provides: BTreeMap<String, Contract>,
    pub requires: BTreeMap<String, Requirement>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Instance {
    pub implementation: String,
    pub version: String,
    pub config: Value,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Target {
    pub plugin: String,
    pub export: String,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Application {
    pub version: u32,
    pub plugins: BTreeMap<String, Instance>,
    pub bindings: BTreeMap<String, BTreeMap<String, Target>>,
    pub entrypoint: Target,
}

#[derive(Debug, Serialize)]
pub struct Plan {
    pub plugins: Vec<Manifest>,
    pub order: Vec<String>,
    pub bindings: BTreeMap<String, BTreeMap<String, String>>,
    pub commands: BTreeMap<String, Vec<String>>,
    pub entrypoint: String,
}

fn reject(message: impl Into<String>) -> Error {
    Error::new("invalid_composition", message)
}

fn name(value: &str) -> bool {
    value.len() <= 48 && manifest::valid_name(value)
}

fn service(plugin: &str, export: &str) -> String {
    format!("{}.{}:{}", plugin.len(), plugin, export)
}

fn valid_contract(contract: &Contract) -> bool {
    manifest::valid_name(&contract.id)
        && contract.major > 0
        && contract.capabilities.len() <= 64
        && contract.capabilities.iter().all(|value| name(value))
}

fn validate_definition(definition: &Definition) -> Result<(), Error> {
    if !name(&definition.implementation)
        || !name(&definition.version)
        || definition.command.is_empty()
        || definition.command.len() > 64
        || definition
            .command
            .iter()
            .any(|arg| arg.is_empty() || arg.len() > 4096 || arg.chars().any(char::is_control))
        || definition.provides.len() > 64
        || definition.requires.len() > 64
        || !definition
            .provides
            .iter()
            .all(|(key, value)| name(key) && valid_contract(value))
        || !definition
            .requires
            .iter()
            .all(|(key, value)| name(key) && valid_contract(&value.contract))
    {
        return Err(reject("invalid plugin definition"));
    }
    Ok(())
}

pub fn compile(application: Application, catalog: Vec<Definition>) -> Result<Plan, Error> {
    if application.version != 1 || application.plugins.is_empty() || catalog.len() > 256 {
        return Err(reject("expected a nonempty application version 1"));
    }
    let mut definitions = BTreeMap::new();
    for definition in catalog {
        validate_definition(&definition)?;
        let key = (
            definition.implementation.clone(),
            definition.version.clone(),
        );
        if definitions.insert(key, definition).is_some() {
            return Err(reject("duplicate implementation version"));
        }
    }
    let mut selected = BTreeMap::new();
    for (id, instance) in &application.plugins {
        if !name(id) || !instance.config.is_object() {
            return Err(reject(format!("invalid instance {id}")));
        }
        let definition = definitions
            .get(&(instance.implementation.clone(), instance.version.clone()))
            .ok_or_else(|| reject(format!("unknown implementation version for {id}")))?;
        selected.insert(id.clone(), definition);
    }
    for (consumer, bindings) in &application.bindings {
        let definition = selected
            .get(consumer)
            .ok_or_else(|| reject(format!("unknown consumer {consumer}")))?;
        if bindings
            .keys()
            .any(|alias| !definition.requires.contains_key(alias))
        {
            return Err(reject(format!("undeclared dependency for {consumer}")));
        }
    }
    let resolve = |target: &Target| -> Result<&Contract, Error> {
        selected
            .get(&target.plugin)
            .and_then(|definition| definition.provides.get(&target.export))
            .ok_or_else(|| {
                reject(format!(
                    "unknown export {}.{}",
                    target.plugin, target.export
                ))
            })
    };
    let entry = resolve(&application.entrypoint)?;
    if entry.id != "application.run" || entry.major != 1 {
        return Err(reject("entrypoint must implement application.run/v1"));
    }
    let mut plugins = Vec::new();
    let mut aliases = BTreeMap::new();
    let mut commands = BTreeMap::new();
    for (id, definition) in &selected {
        let mut resolved = BTreeMap::new();
        for (alias, requirement) in &definition.requires {
            let target = application
                .bindings
                .get(id)
                .and_then(|bindings| bindings.get(alias));
            let Some(target) = target else {
                if requirement.optional {
                    continue;
                }
                return Err(reject(format!("missing binding {id}.{alias}")));
            };
            let provided = resolve(target)?;
            if provided.id != requirement.contract.id
                || provided.major != requirement.contract.major
                || !requirement
                    .contract
                    .capabilities
                    .is_subset(&provided.capabilities)
            {
                return Err(Error::new(
                    "contract_mismatch",
                    format!("incompatible binding {id}.{alias}"),
                ));
            }
            resolved.insert(alias.clone(), service(&target.plugin, &target.export));
        }
        let mut config = application.plugins[id].config.clone();
        config =
            json!({"configuration": config, "bindings": resolved, "command": definition.command});
        plugins.push(Manifest {
            id: id.clone(),
            implementation: definition.implementation.clone(),
            version: definition.version.clone(),
            provides: definition
                .provides
                .keys()
                .map(|export| service(id, export))
                .collect(),
            requires: resolved
                .values()
                .cloned()
                .collect::<BTreeSet<_>>()
                .into_iter()
                .collect(),
            overrides: Vec::new(),
            config,
            concurrency: "concurrent".into(),
            configuration_inputs: BTreeMap::new(),
        });
        aliases.insert(id.clone(), resolved);
        commands.insert(id.clone(), definition.command.clone());
    }
    let order = manifest::order(&plugins, &BTreeSet::new())?;
    Ok(Plan {
        plugins,
        order,
        bindings: aliases,
        commands,
        entrypoint: service(
            &application.entrypoint.plugin,
            &application.entrypoint.export,
        ),
    })
}

#[cfg(test)]
mod tests;
