use std::io;
use std::sync::atomic::{AtomicBool, Ordering};

static STOP_REQUESTED: AtomicBool = AtomicBool::new(false);

pub(crate) fn take_stop_request() -> bool {
    STOP_REQUESTED.swap(false, Ordering::Relaxed)
}

/// Keeps Unix termination signals on the host's normal shutdown path.
pub struct ShutdownSignals {
    #[cfg(unix)]
    previous: Vec<(libc::c_int, libc::sigaction)>,
}

#[cfg(unix)]
extern "C" fn request_stop(_: libc::c_int) {
    STOP_REQUESTED.store(true, Ordering::Relaxed);
}

impl ShutdownSignals {
    /// Install once at the executable boundary, before starting any workers.
    pub fn install() -> io::Result<Self> {
        let signals = Self {
            #[cfg(unix)]
            previous: Vec::new(),
        };
        #[cfg(unix)]
        let mut signals = signals;
        STOP_REQUESTED.store(false, Ordering::Relaxed);
        #[cfg(unix)]
        for signal in [libc::SIGINT, libc::SIGTERM] {
            // The handler only sets an atomic flag. All cleanup runs in the host loop.
            let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
            action.sa_sigaction = request_stop as *const () as usize;
            action.sa_flags = libc::SA_RESTART;
            let mut previous = unsafe { std::mem::zeroed() };
            if unsafe { libc::sigemptyset(&mut action.sa_mask) } != 0
                || unsafe { libc::sigaction(signal, &action, &mut previous) } != 0
            {
                return Err(io::Error::last_os_error());
            }
            signals.previous.push((signal, previous));
        }
        Ok(signals)
    }
}

impl Drop for ShutdownSignals {
    fn drop(&mut self) {
        #[cfg(unix)]
        for (signal, previous) in self.previous.iter().rev() {
            unsafe { libc::sigaction(*signal, previous, std::ptr::null_mut()) };
        }
    }
}
