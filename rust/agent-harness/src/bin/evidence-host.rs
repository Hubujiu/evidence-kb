use std::process::ExitCode;

fn main() -> ExitCode {
    let mut args = std::env::args().skip(1);
    let mut profile = None;
    let mut worker = None;
    let mut stop_file = None;
    while let Some(argument) = args.next() {
        let Some(value) = args.next() else {
            return usage();
        };
        match argument.as_str() {
            "--profile" if profile.is_none() => profile = Some(value),
            "--worker" if worker.is_none() => worker = Some(value),
            "--stop-file" if stop_file.is_none() => stop_file = Some(value),
            _ => return usage(),
        }
    }
    let Some(path) = profile else {
        return usage();
    };
    let outcome = plugin_host::LaunchProfile::load(path).and_then(|mut profile| {
        if let Some(worker) = worker {
            profile.worker[0] = worker;
        }
        if let Some(stop_file) = stop_file {
            profile.stop_file = Some(stop_file.into());
        }
        plugin_host::Host::run(profile, agent_harness::call)
    });
    match outcome {
        Ok(result) => {
            println!("{}", serde_json::to_string(&result).unwrap());
            ExitCode::SUCCESS
        }
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}

fn usage() -> ExitCode {
    eprintln!("usage: evidence-host --profile PATH [--worker EXEC] [--stop-file PATH]");
    ExitCode::from(2)
}
