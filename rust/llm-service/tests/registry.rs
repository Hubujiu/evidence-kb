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
