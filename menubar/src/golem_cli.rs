//! Runs `golem` the way the TUI does, from a GUI app that starts without the shell's PATH.

use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, Instant};

use serde::Deserialize;

use crate::ledger;

const GOLEM_PREFIX: &str = "golem: ";
const ENVIRONMENT_MARKER: &str = "__golem_menubar__";
const DEFAULT_TERMINAL: &str = "Terminal";
const CONFIG_FILE_NAME: &str = "menubar.toml";
const INTERRUPTED_EXIT_STATUS: i32 = 130;
const SHELL_TIMEOUT: Duration = Duration::from_secs(10);
const SHELL_POLL_INTERVAL: Duration = Duration::from_millis(50);
const FALLBACK_DIRECTORIES: [&str; 3] = [".local/bin", "/opt/homebrew/bin", "/usr/local/bin"];

static RUN_COUNTER: AtomicU64 = AtomicU64::new(0);

#[derive(Default, Deserialize)]
#[serde(default)]
struct Config {
    terminal: Option<String>,
}

#[derive(Clone, Debug)]
pub struct Golem {
    /// Absolute path to the golem executable, if one was found.
    pub executable: Option<PathBuf>,
    pub path_variable: String,
    pub terminal_app: String,
}

pub struct CommandOutcome {
    pub is_success: bool,
    /// The first line golem printed, without its `golem: ` prefix.
    pub message: String,
}

fn login_shell() -> String {
    std::env::var("SHELL")
        .ok()
        .filter(|shell| !shell.is_empty())
        .unwrap_or_else(|| "/bin/zsh".to_string())
}

fn config_path() -> PathBuf {
    let base = std::env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .filter(|path| path.is_absolute())
        .unwrap_or_else(|| ledger::home_directory().join(".config"));
    base.join("golem").join(CONFIG_FILE_NAME)
}

fn configured_terminal() -> String {
    let config: Config = std::fs::read_to_string(config_path())
        .ok()
        .and_then(|text| toml::from_str(&text).ok())
        .unwrap_or_default();
    config
        .terminal
        .unwrap_or_else(|| DEFAULT_TERMINAL.to_string())
}

fn scratch_output_path() -> PathBuf {
    let run_id = RUN_COUNTER.fetch_add(1, Ordering::Relaxed);
    std::env::temp_dir().join(format!("golem-menubar-{}-{run_id}.log", std::process::id()))
}

/// Asks the login shell for PATH and golem's location, since launchd gives apps neither.
/// Gives up after `timeout`, so startup files that hang can't leave the app without golem.
fn shell_environment(shell: &str, timeout: Duration) -> Option<(String, String)> {
    let script =
        format!("printf '\\n{ENVIRONMENT_MARKER}%s\\n%s\\n' \"$PATH\" \"$(command -v golem)\"");
    // A file, not a pipe: a job the startup files leave running would hold a pipe open.
    let output_path = scratch_output_path();
    let output = (|| {
        let mut child = Command::new(shell)
            .args(["-lic", &script])
            .stdin(Stdio::null())
            .stdout(std::fs::File::create(&output_path).ok()?)
            .stderr(Stdio::null())
            .spawn()
            .ok()?;
        let deadline = Instant::now() + timeout;
        while child.try_wait().ok()?.is_none() {
            if Instant::now() >= deadline {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
            std::thread::sleep(SHELL_POLL_INTERVAL);
        }
        std::fs::read(&output_path).ok()
    })();
    let _ = std::fs::remove_file(&output_path);
    let output = output?;
    let text = String::from_utf8_lossy(&output);
    let (_, marked) = text.rsplit_once(ENVIRONMENT_MARKER)?;
    let mut lines = marked.lines();
    let path_variable = lines.next()?.to_string();
    let executable = lines.next().unwrap_or("").to_string();
    Some((path_variable, executable))
}

fn fallback_executable(path_variable: &str) -> Option<PathBuf> {
    let home = ledger::home_directory();
    path_variable
        .split(':')
        .map(PathBuf::from)
        .chain(
            FALLBACK_DIRECTORIES
                .iter()
                .map(|directory| home.join(directory)),
        )
        .map(|directory| directory.join("golem"))
        .find(|candidate| candidate.is_file())
}

fn shell_quoted(text: &str) -> String {
    format!("'{}'", text.replace('\'', "'\\''"))
}

fn applescript_quoted(text: &str) -> String {
    format!("\"{}\"", text.replace('\\', "\\\\").replace('"', "\\\""))
}

/// A POSIX shell script that runs `golem pickup` and keeps a failed run's message on screen.
fn pickup_script(executable: &str, path_variable: &str, task_id: &str) -> String {
    format!(
        "PATH={path}; export PATH; {golem} pickup {id}; status=$?; \
         if [ $status -ne 0 ] && [ $status -ne {INTERRUPTED_EXIT_STATUS} ]; then \
         printf 'golem: press Enter to close\\n'; read _; fi",
        path = shell_quoted(path_variable),
        golem = shell_quoted(executable),
        id = shell_quoted(task_id),
    )
}

pub fn first_message(output: &str) -> String {
    let line = output.trim().lines().next().unwrap_or("");
    let message = line.strip_prefix(GOLEM_PREFIX).unwrap_or(line);
    let mut chars = message.chars();
    chars.next().map_or_else(String::new, |first| {
        first.to_uppercase().chain(chars).collect()
    })
}

impl Golem {
    pub fn discover() -> Golem {
        let inherited_path = std::env::var("PATH").unwrap_or_default();
        let shell_environment = shell_environment(&login_shell(), SHELL_TIMEOUT);
        let (path_variable, reported) = shell_environment.unwrap_or_else(|| {
            let home = ledger::home_directory();
            let path_variable = FALLBACK_DIRECTORIES
                .iter()
                .map(|directory| home.join(directory).display().to_string())
                .chain([inherited_path])
                .collect::<Vec<_>>()
                .join(":");
            (path_variable, String::new())
        });
        let executable = Some(PathBuf::from(&reported))
            .filter(|path| path.is_absolute() && path.is_file())
            .or_else(|| fallback_executable(&path_variable));
        Golem {
            executable,
            path_variable,
            terminal_app: configured_terminal(),
        }
    }

    fn missing_outcome() -> CommandOutcome {
        CommandOutcome {
            is_success: false,
            message: "Can't find golem on PATH".to_string(),
        }
    }

    /// Runs `golem <subcommand> <task_id>` to completion and reports its first line of output.
    pub fn run(&self, subcommand: &str, task_id: &str) -> CommandOutcome {
        let Some(executable) = &self.executable else {
            return Golem::missing_outcome();
        };
        // Files, not pipes: an executor that golem starts inherits them and outlives it.
        let output_path = scratch_output_path();
        let result = (|| {
            let output_file = std::fs::File::create(&output_path)?;
            let status = Command::new(executable)
                .args([subcommand, task_id])
                .env("PATH", &self.path_variable)
                .stdin(Stdio::null())
                .stdout(output_file.try_clone()?)
                .stderr(output_file)
                .status()?;
            Ok::<_, std::io::Error>((
                status,
                std::fs::read_to_string(&output_path).unwrap_or_default(),
            ))
        })();
        let _ = std::fs::remove_file(&output_path);
        match result {
            Ok((status, text)) => CommandOutcome {
                is_success: status.success(),
                message: first_message(&text),
            },
            Err(error) => CommandOutcome {
                is_success: false,
                message: error.to_string(),
            },
        }
    }

    /// Opens a terminal window that runs `golem pickup <task_id>` and waits for Enter if it fails.
    pub fn pickup_in_terminal(&self, task_id: &str) -> CommandOutcome {
        let Some(executable) = &self.executable else {
            return Golem::missing_outcome();
        };
        let script = pickup_script(&executable.to_string_lossy(), &self.path_variable, task_id);
        let result = if self.terminal_app == DEFAULT_TERMINAL {
            let command = format!("/bin/sh -c {}", shell_quoted(&script));
            let apple_script = format!(
                "tell application \"Terminal\"\n activate\n do script {}\nend tell",
                applescript_quoted(&command)
            );
            Command::new("osascript")
                .args(["-e", &apple_script])
                .output()
        } else {
            Command::new("open")
                .args([
                    "-na",
                    &self.terminal_app,
                    "--args",
                    "-e",
                    "/bin/sh",
                    "-c",
                    &script,
                ])
                .output()
        };
        match result {
            Ok(output) if output.status.success() => CommandOutcome {
                is_success: true,
                message: String::new(),
            },
            Ok(output) => CommandOutcome {
                is_success: false,
                message: first_message(&String::from_utf8_lossy(&output.stderr)),
            },
            Err(error) => CommandOutcome {
                is_success: false,
                message: error.to_string(),
            },
        }
    }
}

pub fn open_url(url: &str) -> CommandOutcome {
    match Command::new("open").arg(url).output() {
        Ok(output) => CommandOutcome {
            is_success: output.status.success(),
            message: first_message(&String::from_utf8_lossy(&output.stderr)),
        },
        Err(error) => CommandOutcome {
            is_success: false,
            message: error.to_string(),
        },
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn executable_script(directory: &std::path::Path, name: &str, text: &str) -> PathBuf {
        std::fs::create_dir_all(directory).unwrap();
        let path = directory.join(name);
        std::fs::write(&path, text).unwrap();
        Command::new("chmod").arg("+x").arg(&path).status().unwrap();
        path
    }

    #[test]
    fn shell_environment_survives_hung_and_lingering_startup_files() {
        let directory =
            std::env::temp_dir().join(format!("golem-menubar-shell-{}", std::process::id()));
        let hung = executable_script(&directory, "hung", "#!/bin/sh\nexec sleep 5\n");
        let lingering = executable_script(
            &directory,
            "lingering",
            "#!/bin/sh\nsleep 5 &\nexec /bin/sh -c \"$2\"\n",
        );
        let started = Instant::now();
        let hung_result = shell_environment(&hung.to_string_lossy(), Duration::from_millis(200));
        let lingering_result =
            shell_environment(&lingering.to_string_lossy(), Duration::from_secs(4));
        std::fs::remove_dir_all(&directory).unwrap();
        assert_eq!(hung_result, None);
        assert!(lingering_result.is_some_and(|(path_variable, _)| !path_variable.is_empty()));
        assert!(started.elapsed() < Duration::from_secs(4));
    }

    #[test]
    fn pickup_script_runs_golem_with_the_task_id() {
        let directory =
            std::env::temp_dir().join(format!("golem-menubar-test-{}", std::process::id()));
        let fake_golem = executable_script(&directory, "golem's", "#!/bin/sh\necho \"$@\"\n");
        let script = pickup_script(&fake_golem.to_string_lossy(), "/usr/bin:/bin", "a1 b");
        let output = Command::new("/bin/sh")
            .args(["-c", &script])
            .output()
            .unwrap();
        std::fs::remove_dir_all(&directory).unwrap();
        assert_eq!(String::from_utf8_lossy(&output.stdout), "pickup a1 b\n");
    }
}
