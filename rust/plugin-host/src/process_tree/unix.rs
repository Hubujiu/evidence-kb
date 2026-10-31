use std::io;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::process::{Child, Command, ExitStatus};

pub(super) struct Group(Option<libc::pid_t>);

impl Group {
    pub fn prepare(command: &mut Command) -> io::Result<Self> {
        command.process_group(0);
        Ok(Self(None))
    }

    pub fn attach(&mut self, child: &Child) -> io::Result<()> {
        self.0 = Some(
            child
                .id()
                .try_into()
                .map_err(|_| io::Error::other("invalid process group ID"))?,
        );
        Ok(())
    }

    pub fn terminate(&self) -> io::Result<()> {
        if let Some(group) = self.0 {
            let result = unsafe { libc::killpg(group, libc::SIGKILL) };
            if result != 0 {
                let error = io::Error::last_os_error();
                if error.raw_os_error() != Some(libc::ESRCH) {
                    return Err(error);
                }
            }
        }
        Ok(())
    }

    pub fn status(&self) -> io::Result<Option<ExitStatus>> {
        let Some(group) = self.0 else { return Ok(None) };
        let mut status: libc::siginfo_t = unsafe { std::mem::zeroed() };
        if unsafe {
            libc::waitid(
                libc::P_PID,
                group as libc::id_t,
                &mut status,
                libc::WEXITED | libc::WNOHANG | libc::WNOWAIT,
            )
        } != 0
        {
            return Err(io::Error::last_os_error());
        }
        if unsafe { status.si_pid() } == 0 {
            return Ok(None);
        }
        let code = unsafe { status.si_status() };
        let raw = if status.si_code == libc::CLD_EXITED {
            code << 8
        } else if status.si_code == libc::CLD_DUMPED {
            code | 0x80
        } else {
            code
        };
        Ok(Some(ExitStatus::from_raw(raw)))
    }

    pub fn is_empty(&self) -> io::Result<bool> {
        Ok(true)
    }
}
