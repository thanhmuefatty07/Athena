use serde::{Deserialize, Serialize};

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub enum QosProfile {
    Interactive,
    Worker,
    Background,
}

impl QosProfile {
    pub fn parse(raw: &str) -> Result<Self, String> {
        match raw.trim().to_ascii_lowercase().as_str() {
            "interactive" | "mode_a" => Ok(Self::Interactive),
            "worker" | "p_core" | "user_initiated" => Ok(Self::Worker),
            "background" | "e_core" => Ok(Self::Background),
            other => Err(format!(
                "unsupported --qos profile '{other}' (expected interactive|worker|background)"
            )),
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Self::Interactive => "interactive",
            Self::Worker => "worker",
            Self::Background => "background",
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct QosStatus {
    pub profile: &'static str,
    pub os_mechanism: &'static str,
    pub applied: bool,
    pub detail: String,
}

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct OsMemorySnapshot {
    pub phys_footprint_bytes: u64,
    pub resident_or_pss_bytes: u64,
    pub private_dirty_bytes: u64,
    pub max_rss_bytes: u64,
    pub source: &'static str,
}

pub fn apply_qos_profile(profile: QosProfile, linux_cpu_slot: Option<usize>) -> QosStatus {
    #[cfg(target_os = "macos")]
    {
        let _ = linux_cpu_slot;
        let class = match profile {
            QosProfile::Interactive => libc::qos_class_t::QOS_CLASS_USER_INTERACTIVE,
            QosProfile::Worker => libc::qos_class_t::QOS_CLASS_USER_INITIATED,
            QosProfile::Background => libc::qos_class_t::QOS_CLASS_BACKGROUND,
        };
        // SAFETY: pthread_set_qos_class_self_np modifies only the calling thread's Mach scheduling QoS class.
        let rc = unsafe { libc::pthread_set_qos_class_self_np(class, 0) };
        QosStatus {
            profile: profile.as_str(),
            os_mechanism: "pthread_set_qos_class_self_np",
            applied: rc == 0,
            detail: format!("rc={rc}"),
        }
    }

    #[cfg(target_os = "linux")]
    {
        let slot = linux_cpu_slot.unwrap_or(0);
        // SAFETY: Zeroed cpu_set_t with single valid CPU index via CPU_SET and sched_setaffinity on tid=0 (self).
        unsafe {
            let mut set: libc::cpu_set_t = std::mem::zeroed();
            let nprocs = libc::sysconf(libc::_SC_NPROCESSORS_ONLN).max(1) as usize;
            let pinned = slot % nprocs;
            libc::CPU_ZERO(&mut set);
            libc::CPU_SET(pinned, &mut set);
            let rc = libc::sched_setaffinity(0, std::mem::size_of::<libc::cpu_set_t>(), &set);
            QosStatus {
                profile: profile.as_str(),
                os_mechanism: "sched_setaffinity",
                applied: rc == 0,
                detail: format!("cpu={pinned},rc={rc}"),
            }
        }
    }

    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        let _ = linux_cpu_slot;
        QosStatus {
            profile: profile.as_str(),
            os_mechanism: "unsupported_host",
            applied: false,
            detail: "no platform QoS syscall".into(),
        }
    }
}

pub fn read_os_memory_snapshot() -> OsMemorySnapshot {
    let max_rss_bytes = read_rusage_max_rss_bytes();

    #[cfg(target_os = "macos")]
    {
        // Verified against MacOSX.sdk/usr/include/mach/task_info.h:
        // TASK_VM_INFO = 22, TASK_VM_INFO_REV1_COUNT = 38, TASK_VM_INFO_COUNT = 93 (u32 words),
        // offsetof(resident_size) = 16 bytes (words 4..6), offsetof(phys_footprint) = 144 bytes (words 36..38).
        const TASK_VM_INFO_FLAVOR: libc::task_flavor_t = 22;
        const FULL_WORDS: usize = 93;
        const REV1_WORDS: libc::mach_msg_type_number_t = 38;

        let mut words = [0u32; FULL_WORDS];
        let mut count = FULL_WORDS as libc::mach_msg_type_number_t;
        // SAFETY: `words` holds 93 `natural_t` (`u32`) words matching `TASK_VM_INFO_COUNT`; kernel writes at most `count` words.
        let kr = unsafe {
            #[allow(deprecated)]
            let self_port = libc::mach_task_self();
            libc::task_info(
                self_port,
                TASK_VM_INFO_FLAVOR,
                words.as_mut_ptr().cast::<libc::integer_t>(),
                &mut count,
            )
        };
        if kr == 0 && count >= REV1_WORDS {
            let read_u64 = |word_idx: usize| -> u64 {
                let mut b = [0u8; 8];
                b[..4].copy_from_slice(&words[word_idx].to_ne_bytes());
                b[4..].copy_from_slice(&words[word_idx + 1].to_ne_bytes());
                u64::from_ne_bytes(b)
            };
            let resident = read_u64(4);
            let phys_footprint = read_u64(36);
            return OsMemorySnapshot {
                phys_footprint_bytes: phys_footprint,
                resident_or_pss_bytes: resident,
                private_dirty_bytes: phys_footprint,
                max_rss_bytes,
                source: "mach_task_vm_info",
            };
        }
        OsMemorySnapshot {
            phys_footprint_bytes: max_rss_bytes,
            resident_or_pss_bytes: max_rss_bytes,
            private_dirty_bytes: 0,
            max_rss_bytes,
            source: "rusage_fallback",
        }
    }

    #[cfg(target_os = "linux")]
    {
        if let Ok(rollup) = std::fs::read_to_string("/proc/self/smaps_rollup") {
            let mut rss_kib = 0u64;
            let mut pss_kib = 0u64;
            let mut priv_dirty_kib = 0u64;
            for line in rollup.lines() {
                let parse_kib = |s: &str| -> u64 {
                    s.split_whitespace()
                        .nth(1)
                        .and_then(|v| v.parse::<u64>().ok())
                        .unwrap_or(0)
                };
                if line.starts_with("Rss:") {
                    rss_kib = parse_kib(line);
                } else if line.starts_with("Pss:") {
                    pss_kib = parse_kib(line);
                } else if line.starts_with("Private_Dirty:") {
                    priv_dirty_kib = parse_kib(line);
                }
            }
            let pss_bytes = pss_kib.saturating_mul(1024);
            let rss_bytes = rss_kib.saturating_mul(1024);
            let dirty_bytes = priv_dirty_kib.saturating_mul(1024);
            return OsMemorySnapshot {
                phys_footprint_bytes: if pss_bytes > 0 { pss_bytes } else { rss_bytes },
                resident_or_pss_bytes: if pss_bytes > 0 { pss_bytes } else { rss_bytes },
                private_dirty_bytes: dirty_bytes,
                max_rss_bytes,
                source: "proc_self_smaps_rollup",
            };
        }
        OsMemorySnapshot {
            phys_footprint_bytes: max_rss_bytes,
            resident_or_pss_bytes: max_rss_bytes,
            private_dirty_bytes: 0,
            max_rss_bytes,
            source: "rusage_fallback",
        }
    }

    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        OsMemorySnapshot {
            phys_footprint_bytes: max_rss_bytes,
            resident_or_pss_bytes: max_rss_bytes,
            private_dirty_bytes: 0,
            max_rss_bytes,
            source: "unsupported",
        }
    }
}

fn read_rusage_max_rss_bytes() -> u64 {
    // SAFETY: zero-initialized libc::rusage populated by getrusage(RUSAGE_SELF, ...).
    unsafe {
        let mut usage: libc::rusage = std::mem::zeroed();
        if libc::getrusage(libc::RUSAGE_SELF, &mut usage) == 0 {
            let raw = usage.ru_maxrss.max(0) as u64;
            #[cfg(target_os = "macos")]
            {
                raw // Darwin ru_maxrss is already in bytes
            }
            #[cfg(not(target_os = "macos"))]
            {
                raw.saturating_mul(1024) // Linux ru_maxrss is in KiB
            }
        } else {
            0
        }
    }
}
