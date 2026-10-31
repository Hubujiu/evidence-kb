use std::io;
use std::process::{Child, Command, ExitStatus};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(unix)]
mod unix;
#[cfg(windows)]
mod windows;
#[cfg(unix)]
use unix::Group;
#[cfg(windows)]
use windows::Group;

pub(crate) struct ProcessTree {
    pub child: Child,
    group: Group,
    closed: bool,
}

impl ProcessTree {
    pub fn spawn(command: &mut Command) -> io::Result<Self> {
        let group = Group::prepare(command)?;
        let child = command.spawn()?;
        let mut tree = Self {
            child,
            group,
            closed: false,
        };
        tree.group.attach(&tree.child)?;
        Ok(tree)
    }

    pub fn try_wait(&mut self) -> io::Result<Option<ExitStatus>> {
        #[cfg(windows)]
        {
            self.child.try_wait()
        }
        #[cfg(unix)]
        {
            self.group.status()
        }
    }

    pub fn shutdown(&mut self, deadline: Instant) -> io::Result<()> {
        if self.closed {
            return Ok(());
        }
        let termination = self.group.terminate();
        if !matches!(self.child.try_wait(), Ok(Some(_))) {
            let _ = self.child.kill();
        }
        loop {
            if self.child.try_wait()?.is_some() && self.group.is_empty()? {
                self.closed = true;
                return termination;
            }
            if Instant::now() >= deadline {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "worker process tree did not exit",
                ));
            }
            thread::sleep(Duration::from_millis(10));
        }
    }
}

impl Drop for ProcessTree {
    fn drop(&mut self) {
        let _ = self.shutdown(Instant::now() + Duration::from_secs(2));
    }
}
