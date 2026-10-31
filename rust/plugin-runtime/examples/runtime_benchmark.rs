use plugin_runtime::{Coordinator, Error, PROTOCOL};
use serde_json::{Value, json};
use std::hint::black_box;
use std::sync::{Arc, Mutex};
use std::time::Instant;

#[derive(Default)]
struct Harness {
    runtime: Coordinator,
    commands: u64,
    callbacks: u64,
}

impl Harness {
    fn request(&mut self, command: &str, fields: Value) -> Result<Value, Error> {
        self.commands += 1;
        let mut fields = fields.as_object().unwrap().clone();
        fields.insert("protocol".into(), json!(PROTOCOL));
        fields.insert("command".into(), json!(format!("runtime_{command}")));
        self.runtime.request(Value::Object(fields))
    }

    fn complete(&mut self, action: &Value, services: Value) -> Result<(), Error> {
        self.request("complete", json!({"host":action["host"],"operation":action["operation"],"status":"ok","services":services}))?;
        self.request(
            "ack",
            json!({"host":action["host"],"operations":[action["operation"]]}),
        )?;
        Ok(())
    }

    fn pump(&mut self, host: u64) -> Result<(), Error> {
        loop {
            let response = self.request("poll", json!({"host":host}))?;
            let actions = response["actions"].as_array().unwrap();
            if actions.is_empty() {
                break;
            }
            for action in actions {
                match action["kind"].as_str().unwrap() {
                    "init" => {
                        self.callbacks += 1;
                        let plugin = action["plugin"].as_str().unwrap();
                        let service = if let Some(index) = plugin.strip_prefix("provider-") {
                            self.request("listener", json!({"host":action["host"],"operation":action["operation"],"event":"bench.event"}))?;
                            format!("service-{index}")
                        } else {
                            "dependent".into()
                        };
                        black_box(self.callbacks);
                        self.complete(action, json!([service]))?;
                    }
                    "cleanup" => {
                        self.callbacks += 1;
                        black_box(self.callbacks);
                        self.complete(action, json!([]))?;
                    }
                    "cancel" => (),
                    kind => return Err(Error::new("unexpected_action", kind)),
                }
            }
        }
        Ok(())
    }

    fn create(&mut self, size: usize) -> Result<u64, Error> {
        let plugins: Vec<_> = (0..size).map(|index| json!({"id":format!("provider-{index}"),"version":"1","provides":[format!("service-{index}")],"requires":[],"concurrency":"concurrent"})).collect();
        let host = self.request("create", json!({"plugins":plugins}))?["host"]
            .as_u64()
            .unwrap();
        self.pump(host)?;
        Ok(host)
    }

    fn invoke(&mut self, host: u64, registration: u64) -> Result<(), Error> {
        let action = self.request(
            "invoke",
            json!({"host":host,"registration":registration,"method":"read"}),
        )?;
        self.callbacks += 1;
        black_box(self.callbacks.wrapping_mul(17));
        self.complete(&action, json!([]))?;
        Ok(())
    }

    fn close(&mut self, host: u64) -> Result<Value, Error> {
        self.request("drain", json!({"host":host}))?;
        self.pump(host)?;
        let result = self.request("collect", json!({"host":host}))?;
        if result["snapshot"]["state"] != "disposed" {
            return Err(Error::new("cleanup_incomplete", "scope was not disposed"));
        }
        Ok(result["remaining"].clone())
    }
}

fn timed<F>(samples: usize, mut action: F) -> (Vec<u64>, Vec<Value>, u64)
where
    F: FnMut(usize) -> Result<(), Error>,
{
    let started = Instant::now();
    let mut durations = Vec::with_capacity(samples);
    let mut errors = Vec::new();
    for sample in 0..samples {
        let begin = Instant::now();
        let result = action(sample);
        durations.push(begin.elapsed().as_nanos() as u64);
        if let Err(error) = result {
            errors.push(json!({"sample":sample,"code":error.code,"message":error.message}));
        }
    }
    (durations, errors, started.elapsed().as_nanos() as u64)
}

fn row(
    name: &str,
    size: usize,
    samples: usize,
    mut action: impl FnMut(&mut Harness, u64, usize) -> Result<(), Error>,
    expected: usize,
) -> Value {
    let mut harness = Harness::default();
    let host = harness.create(size).unwrap();
    let warmup = samples.min(20);
    let warmup_errors: Vec<_> = (0..warmup)
        .filter_map(|sample| {
            action(&mut harness, host, sample)
                .err()
                .map(|error| json!({"sample":sample,"code":error.code,"message":error.message}))
        })
        .collect();
    let before_commands = harness.commands;
    let before_callbacks = harness.callbacks;
    let (durations, mut errors, elapsed) = timed(samples, |sample| {
        action(&mut harness, host, sample + warmup)
    });
    let operation_count = harness.commands - before_commands;
    let callbacks = harness.callbacks - before_callbacks;
    if callbacks != (expected * samples) as u64 {
        errors.push(json!({"sample":null,"code":"callback_count","expected":expected*samples,"actual":callbacks}));
    }
    let residuals = harness.close(host).unwrap();
    if residuals
        .as_object()
        .unwrap()
        .values()
        .any(|count| count != 0)
    {
        errors.push(json!({"sample":null,"code":"resource_residual","counts":residuals}));
    }
    json!({"workload":name,"catalog_size":size,"concurrency":1,"samples":samples,"durations_ns":durations,"elapsed_ns":elapsed,
        "warmup_samples":warmup,"warmup_errors":warmup_errors,"errors":errors,"operation_count":operation_count,
        "expected_callbacks":expected*samples,"actual_callbacks":callbacks,"resource_residuals":residuals})
}

fn cold(size: usize, samples: usize) -> Value {
    let mut command_count = 0;
    let mut callbacks = 0;
    let mut residuals = json!({});
    let (durations, errors, elapsed) = timed(samples, |_| {
        let mut harness = Harness::default();
        let host = harness.create(size)?;
        residuals = harness.close(host)?;
        command_count += harness.commands;
        callbacks += harness.callbacks;
        Ok(())
    });
    json!({"workload":"cold_create_and_dispose","catalog_size":size,"concurrency":1,"samples":samples,"durations_ns":durations,"elapsed_ns":elapsed,
        "warmup_samples":0,"warmup_errors":[],"errors":errors,"operation_count":command_count,"expected_callbacks":size*samples*3,
        "actual_callbacks":callbacks,"resource_residuals":residuals})
}

fn dependency(size: usize, samples: usize) -> Value {
    let mut harness = Harness::default();
    let host = harness.create(size).unwrap();
    let child = harness.request("create", json!({"parent":host,"plugins":[{"id":"consumer","version":"1","provides":["dependent"],"requires":["service-0"],"concurrency":"concurrent"}]})).unwrap()["host"].as_u64().unwrap();
    harness.pump(host).unwrap();
    let unaffected = if size > 1 {
        Some(
            harness
                .request("resolve", json!({"host":host,"service":"service-1"}))
                .unwrap(),
        )
    } else {
        None
    };
    let before_commands = harness.commands;
    let before_callbacks = harness.callbacks;
    let (durations, mut errors, elapsed) = timed(samples, |sample| {
        harness.request(
            "update",
            json!({"host":host,"plugin":"provider-0","config":{"revision":sample+1}}),
        )?;
        harness.pump(host)?;
        let root = harness.request("inspect", json!({"host":host}))?;
        let dependent = harness.request("inspect", json!({"host":child}))?;
        if root["state"] != "ready" || dependent["state"] != "ready" {
            return Err(Error::new(
                "dependency_restart",
                "affected instances did not return to ready",
            ));
        }
        if let Some(binding) = &unaffected
            && harness.request("resolve", json!({"host":host,"service":"service-1"}))? != *binding
        {
            return Err(Error::new(
                "unrelated_restart",
                "unrelated registration changed",
            ));
        }
        Ok(())
    });
    let callbacks = harness.callbacks - before_callbacks;
    let operation_count = harness.commands - before_commands;
    if callbacks != (samples * 5) as u64 {
        errors.push(
            json!({"sample":null,"code":"callback_count","expected":samples*5,"actual":callbacks}),
        );
    }
    let residuals = harness.close(host).unwrap();
    json!({"workload":"dependency_replace_and_reactivate","catalog_size":size,"concurrency":1,"samples":samples,"durations_ns":durations,"elapsed_ns":elapsed,
        "warmup_samples":0,"warmup_errors":[],"errors":errors,"operation_count":operation_count,"expected_callbacks":samples*5,
        "actual_callbacks":callbacks,"resource_residuals":residuals})
}

fn concurrent(size: usize, samples: usize, concurrency: usize) -> Value {
    let mut harness = Harness::default();
    let host = harness.create(size).unwrap();
    let registration = harness
        .request("resolve", json!({"host":host,"service":"service-0"}))
        .unwrap()["registration"]
        .as_u64()
        .unwrap();
    let before_commands = harness.commands;
    let before_callbacks = harness.callbacks;
    let shared = Arc::new(Mutex::new(harness));
    let count = samples.div_ceil(concurrency);
    let begin = Instant::now();
    let outputs = std::thread::scope(|scope| {
        let mut workers = Vec::new();
        let barrier = Arc::new(std::sync::Barrier::new(concurrency));
        for _ in 0..concurrency {
            let shared = shared.clone();
            let barrier = barrier.clone();
            workers.push(scope.spawn(move || {
                barrier.wait();
                timed(count, |_| {
                    let action = shared.lock().unwrap().request(
                        "invoke",
                        json!({"host":host,"registration":registration,"method":"read"}),
                    )?;
                    black_box(action["operation"].as_u64().unwrap().wrapping_mul(17));
                    let mut harness = shared.lock().unwrap();
                    harness.callbacks += 1;
                    harness.complete(&action, json!([]))
                })
            }));
        }
        workers
            .into_iter()
            .map(|worker| worker.join().unwrap())
            .collect::<Vec<_>>()
    });
    let elapsed = begin.elapsed().as_nanos() as u64;
    let mut durations = Vec::new();
    let mut errors = Vec::new();
    for (worker, (samples, failures, _)) in outputs.into_iter().enumerate() {
        durations.extend(samples);
        errors.extend(
            failures
                .into_iter()
                .map(|failure| json!({"worker":worker,"failure":failure})),
        );
    }
    let mut harness = Arc::try_unwrap(shared).ok().unwrap().into_inner().unwrap();
    let callbacks = harness.callbacks - before_callbacks;
    let operation_count = harness.commands - before_commands;
    if callbacks != (count * concurrency) as u64 {
        errors
            .push(json!({"code":"callback_count","expected":count*concurrency,"actual":callbacks}));
    }
    let residuals = harness.close(host).unwrap();
    json!({"workload":"concurrent_invocation","catalog_size":size,"concurrency":concurrency,"samples":count*concurrency,"requested_samples":samples,
        "durations_ns":durations,"elapsed_ns":elapsed,"warmup_samples":0,"warmup_errors":[],"errors":errors,"operation_count":operation_count,
        "expected_callbacks":count*concurrency,"actual_callbacks":callbacks,"resource_residuals":residuals})
}

fn main() {
    let arguments: Vec<_> = std::env::args().collect();
    let samples = arguments
        .windows(2)
        .find(|args| args[0] == "--samples")
        .map_or(1000, |args| args[1].parse::<usize>().unwrap());
    assert!(samples > 0 && samples <= 100_000);
    let mut rows = Vec::new();
    for size in [1, 32, 128, 256] {
        rows.push(cold(size, samples.min(20)));
        rows.push(row(
            "steady_invocation",
            size,
            samples,
            |harness, host, _| {
                let registration = harness
                    .request("resolve", json!({"host":host,"service":"service-0"}))?["registration"]
                    .as_u64()
                    .unwrap();
                harness.invoke(host, registration)
            },
            1,
        ));
        rows.push(row("scope_create_call_dispose", size, samples.min(100), |harness, host, _| {
            let child = harness.request("create", json!({"parent":host,"plugins":[]}))?["host"].as_u64().unwrap();
            let registration = harness.request("resolve", json!({"host":child,"service":"service-0"}))?["registration"].as_u64().unwrap();
            harness.invoke(child, registration)?;
            harness.close(child)?;
            Ok(())
        }, 1));
        rows.push(dependency(size, samples.min(100)));
        rows.push(row("listener_selection_and_owned_callbacks", size, samples, |harness, host, _| {
            let lease = harness.request("acquire", json!({"host":host}))?["lease"].clone();
            let listeners = harness.request("listeners", json!({"host":host,"event":"bench.event"}))?["listeners"].as_array().unwrap().clone();
            for listener in listeners {
                let owner = harness.request("acquire", json!({"host":host,"instance":listener["instance"],"generation":listener["generation"],"parent_lease":lease}))?["lease"].clone();
                harness.callbacks += 1;
                black_box(harness.callbacks.wrapping_mul(17));
                harness.request("release", json!({"host":host,"lease":owner}))?;
            }
            harness.request("release", json!({"host":host,"lease":lease}))?;
            Ok(())
        }, size));
        for concurrency in [1, 4, 16] {
            rows.push(concurrent(size, samples, concurrency));
        }
    }
    println!(
        "{}",
        json!({"protocol":"plugin-runtime-benchmark/1","comparison":"new_core_characterization_only","clock":"std::time::Instant","timed_boundary":"JSON coordinator request through completed and acknowledged native effect; cold includes disposal; concurrency includes lock wait","rows":rows})
    );
}
