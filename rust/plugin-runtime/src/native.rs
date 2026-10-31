use tokio::sync::watch;
use tokio::task::JoinHandle;

pub struct OwnedTask {
    resource: u64,
    stop: watch::Sender<bool>,
    join: JoinHandle<()>,
}

impl OwnedTask {
    pub fn spawn<F, Fut>(resource: u64, task: F) -> Self
    where
        F: FnOnce(watch::Receiver<bool>) -> Fut,
        Fut: Future<Output = ()> + Send + 'static,
    {
        let (stop, receiver) = watch::channel(false);
        Self {
            resource,
            stop,
            join: tokio::spawn(task(receiver)),
        }
    }

    pub fn resource(&self) -> u64 {
        self.resource
    }

    pub fn request_stop(&self) {
        self.stop.send_replace(true);
    }

    pub fn is_finished(&self) -> bool {
        self.join.is_finished()
    }

    pub async fn finish(self) -> Result<(), tokio::task::JoinError> {
        self.request_stop();
        self.join.await
    }
}
