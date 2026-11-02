use plugin_host::process_plugin::{CallContext, Plugin, PluginError, serve};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::io;
use std::sync::Mutex;

#[derive(Default)]
struct Greeting(Mutex<(String, bool)>);

impl Plugin for Greeting {
    fn services(&self) -> BTreeMap<String, Vec<String>> {
        BTreeMap::from([("value".into(), vec!["get".into()])])
    }

    fn configure(&self, configuration: Value) -> Result<(), PluginError> {
        if configuration.as_object().is_none_or(|value| {
            value
                .keys()
                .any(|key| !["greeting", "error"].contains(&key.as_str()))
        }) {
            return Err(PluginError::new(
                "INVALID_CONFIGURATION",
                "expected optional greeting string and error flag",
            ));
        }
        let greeting = configuration
            .get("greeting")
            .map_or(Some("world"), Value::as_str)
            .ok_or_else(|| {
                PluginError::new("INVALID_CONFIGURATION", "greeting must be a string")
            })?;
        let error = configuration
            .get("error")
            .map_or(Some(false), Value::as_bool)
            .ok_or_else(|| PluginError::new("INVALID_CONFIGURATION", "error must be a boolean"))?;
        *self.0.lock().unwrap() = (greeting.to_owned(), error);
        Ok(())
    }

    fn invoke(
        &self,
        _: &str,
        _: &str,
        request: Value,
        context: CallContext,
    ) -> Result<Value, PluginError> {
        context.check()?;
        if request != json!({}) {
            return Err(PluginError::new(
                "CONTRACT_REQUEST_INVALID",
                "expected an empty object",
            ));
        }
        let configuration = self.0.lock().unwrap();
        if configuration.1 {
            return Err(PluginError::new(
                "CONTRACT_PROVIDER_UNAVAILABLE",
                "Greeting provider unavailable",
            )
            .with_status(503)
            .with_details(json!({"retryable": true})));
        }
        Ok(json!(configuration.0))
    }
}

fn main() -> io::Result<()> {
    serve(io::stdin().lock(), io::stdout(), Greeting::default())
}
