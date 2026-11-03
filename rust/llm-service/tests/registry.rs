use llm_service::{Registry, RoutePhase};
use std::sync::{
    Arc, Weak,
    atomic::{AtomicUsize, Ordering},
};

struct InspectsOnDrop {
    registry: Weak<Registry<InspectsOnDrop>>,
    dropped: Arc<AtomicUsize>,
}

impl Drop for InspectsOnDrop {
    fn drop(&mut self) {
        if let Some(registry) = self.registry.upgrade() {
            assert!(registry.inspect().unwrap().is_empty());
        }
        self.dropped.fetch_add(1, Ordering::SeqCst);
    }
}

#[test]
fn adapter_destruction_can_reenter_registry_for_detach_cleanup_and_final_admission_release() {
    for mode in 0..3 {
        let registry = Arc::new(Registry::default());
        let dropped = Arc::new(AtomicUsize::new(0));
        let provider = Arc::new(InspectsOnDrop {
            registry: Arc::downgrade(&registry),
            dropped: dropped.clone(),
        });
        let binding = registry.register("route", provider, None).unwrap();
        match mode {
            0 => registry.detach("route", binding.generation).unwrap(),
            1 => {
                drop(registry.begin_cleanup("route", binding).unwrap());
                registry.finish_cleanup(binding, None).unwrap();
            }
            _ => {
                let admitted = registry.prepare("route", None).unwrap().admit().unwrap();
                registry.detach("route", binding.generation).unwrap();
                assert_eq!(dropped.load(Ordering::SeqCst), 0);
                drop(admitted);
            }
        }
        assert_eq!(dropped.load(Ordering::SeqCst), 1);
    }
}

#[test]
fn cleanup_waits_for_the_admission_and_drain_invalidates_earlier_preparations() {
    let registry = Registry::<str>::default();
    let binding = registry
        .register("route", Arc::from("provider configuration"), None)
        .unwrap();
    let prepared = registry.prepare("route", None).unwrap();
    let admitted = registry.prepare("route", None).unwrap().admit().unwrap();
    assert_eq!(
        registry.begin_cleanup("route", binding).err().unwrap().code,
        "LLM_BUSY"
    );
    assert_eq!(registry.inspect().unwrap()[0].phase, RoutePhase::Draining);
    assert_eq!(prepared.admit().err().unwrap().code, "LLM_BINDING_CHANGED");
    assert_eq!(
        admitted.snapshot().payload.as_ref(),
        "provider configuration"
    );
    drop(admitted);
    assert_eq!(
        registry
            .begin_cleanup("route", binding)
            .unwrap()
            .unwrap()
            .active,
        0
    );
    registry.finish_cleanup(binding, None).unwrap();
    assert!(registry.inspect().unwrap().is_empty());
}

#[test]
fn adoption_changes_generation_not_registration_and_stale_prepared_snapshots_remain_owned() {
    use llm_service::RegistrationIdentity;
    use serde_json::json;
    let registry = Registry::<str>::default();
    let payload: Arc<str> = Arc::from("owned adapter");
    let retained = Arc::downgrade(&payload);
    let original = registry.register("route", payload, None).unwrap();
    let stale = registry.prepare("route", None).unwrap();
    let snapshot = registry.prepare("route", None).unwrap();
    let adopted = registry
        .adopt(vec![(
            original.registration,
            RegistrationIdentity {
                registration_id: json!("adopted"),
                owner: None,
            },
        )])
        .unwrap()
        .remove(0)
        .binding;
    assert_eq!(adopted.registration, original.registration);
    assert_ne!(adopted.generation, original.generation);
    assert_eq!(stale.admit().err().unwrap().code, "LLM_BINDING_CHANGED");
    assert!(registry.begin_cleanup("route", original).unwrap().is_none());
    let admitted = registry.prepare("route", None).unwrap().admit().unwrap();
    assert_eq!(admitted.snapshot().binding, adopted);
    assert_eq!(
        registry
            .adopt(vec![(
                adopted.registration,
                RegistrationIdentity {
                    registration_id: json!("while-admitted"),
                    owner: None,
                }
            )])
            .err()
            .unwrap()
            .code,
        "LLM_BUSY"
    );
    drop(admitted);
    registry.detach("route", adopted.generation).unwrap();
    assert!(registry.inspect().unwrap().is_empty());
    assert!(retained.upgrade().is_some());
    drop(snapshot);
    assert!(retained.upgrade().is_none());
}

#[test]
fn releasing_detached_old_admission_cannot_remove_the_replacement_route() {
    let registry = Registry::<str>::default();
    let old: Arc<str> = Arc::from("old");
    let retained = Arc::downgrade(&old);
    let original = registry.register("route", old, None).unwrap();
    let admission = registry.prepare("route", None).unwrap().admit().unwrap();
    registry.detach("route", original.generation).unwrap();
    let replacement = registry.register("route", Arc::from("new"), None).unwrap();
    assert_eq!(
        registry
            .detach("route", original.generation)
            .unwrap_err()
            .code,
        "LLM_BINDING_CHANGED"
    );
    assert!(retained.upgrade().is_some());
    drop(admission);
    assert!(retained.upgrade().is_none());
    let current = registry.prepare("route", None).unwrap();
    assert_eq!(current.snapshot().binding, replacement);
    assert_eq!(current.snapshot().payload.as_ref(), "new");
    assert_eq!(registry.inspect().unwrap().len(), 1);
}

#[test]
fn cleanup_claim_is_exclusive_and_failed_cleanup_remains_owned_for_explicit_retry() {
    use std::sync::Barrier;
    struct Cleanup {
        calls: AtomicUsize,
        entered: Barrier,
        release: Barrier,
    }
    impl Cleanup {
        fn close(&self) -> Result<(), &'static str> {
            if self.calls.fetch_add(1, Ordering::SeqCst) == 0 {
                self.entered.wait();
                self.release.wait();
                Err("synthetic cleanup failure")
            } else {
                Ok(())
            }
        }
    }
    let registry = Arc::new(Registry::default());
    let payload = Arc::new(Cleanup {
        calls: AtomicUsize::new(0),
        entered: Barrier::new(2),
        release: Barrier::new(2),
    });
    let binding = registry.register("route", payload.clone(), None).unwrap();
    let worker = std::thread::spawn({
        let registry = registry.clone();
        move || {
            let snapshot = registry.begin_cleanup("route", binding).unwrap().unwrap();
            let error = snapshot.payload.close().err().map(str::to_owned);
            registry.finish_cleanup(binding, error).unwrap();
        }
    });
    payload.entered.wait();
    let competing = registry
        .begin_cleanup("route", binding)
        .err()
        .map(|error| error.code);
    let closing = registry.begin_close().unwrap();
    let late = registry
        .register("late", payload.clone(), None)
        .err()
        .map(|error| error.code);
    payload.release.wait();
    worker.join().unwrap();
    assert_eq!(competing.as_deref(), Some("LLM_CLEANUP_BUSY"));
    assert_eq!(late.as_deref(), Some("LLM_PROVIDER_ALREADY_REGISTERED"));
    assert_eq!(closing[0].phase, RoutePhase::Cleaning);
    assert_eq!(payload.calls.load(Ordering::SeqCst), 1);
    let failed = registry.inspect().unwrap();
    assert_eq!(failed[0].phase, RoutePhase::CleanupFailed);
    assert_eq!(
        failed[0].cleanup_error.as_deref(),
        Some("synthetic cleanup failure")
    );
    assert_eq!(registry.finish_close().unwrap_err().code, "LLM_BUSY");
    let retry = registry.begin_cleanup("route", binding).unwrap().unwrap();
    retry.payload.close().unwrap();
    registry.finish_cleanup(binding, None).unwrap();
    registry.finish_close().unwrap();
    assert_eq!(payload.calls.load(Ordering::SeqCst), 2);
    assert!(registry.inspect().unwrap().is_empty());
}
