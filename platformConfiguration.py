"""Installation-only configuration, safe operational logs, and proxy trust."""

import ipaddress
import logging
import os
import shutil
import tempfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
import threading


configurationDefaults = {
    "enforceEmailUsernames": False,
    "trustedProxyAddresses": "",
    "allowWebRegistration": False,
    "smtpEnabled": False,
    "smtpHost": "",
    "smtpPort": None,
    "smtpSecurity": "starttls",
    "smtpUsername": "",
    "smtpPassword": "",
    "smtpFromAddress": "",
    "smtpFromName": "",
    "smtpTimeoutSeconds": 10,
    "requireRegistrationVerification": False,
}

extraConfigurationHeader = "# --- Extra data (ignored) ---"
extraConfigurationDescription = "# Entries in this section are preserved but are not applied."


class ConfigurationError(ValueError):
    """Contains setting names and safe instructions, never supplied values."""


def configurationSample():
    lines = [
        "# No Part Finance server configuration",
        "# Rename platform.conf.sample to platform.conf, then restart the server.",
        "# Missing settings use these defaults. Only full-line # comments are supported.",
        "# Startup refreshes this sample and updates an existing platform.conf.",
        "# Configured values and commented settings are kept; new settings use defaults.",
        "# A missing platform.conf is never created automatically.",
        "# For repeated settings, the last active value above Extra data wins.",
        "# Comments are retained; unrecognized entries move to Extra data below.",
        "# SECURITY: This file can contain your SMTP password. Keep it private.",
        "# Linux: restrict permissions (for example chmod 600 platform.conf) so only",
        "# the administrator/application account can read or change it.",
        "# Windows: use Properties > Security to restrict access to those accounts.",
        "# Use a dedicated, revocable SMTP/app password where your provider supports it.",
        "# Never share your real file. CLI packages can include it and private logs;",
        "# handle those ZIP files as private installation copies. Browser ZIPs omit both.",
        "",
    ]
    sections = {
        "enforceEmailUsernames": ("Account usernames", "Choose whether usernames must use an email address format."),
        "trustedProxyAddresses": ("Reverse proxy", "Identify trusted proxies that forward the visitor's IP and HTTPS status."),
        "allowWebRegistration": ("Web registration", "Choose whether visitors can create their own user accounts."),
        "smtpEnabled": ("Email delivery (SMTP)", "Configure the mail server used to send verification codes."),
        "requireRegistrationVerification": ("Email verification", "Choose whether users must verify their email before account access."),
    }
    for key, value in configurationDefaults.items():
        if key in sections:
            title, description = sections[key]
            lines.extend(["", f"# --- {title} ---", f"# {description}", ""])
        if key == "trustedProxyAddresses":
            lines.extend([
                "# Enter NGINX's internal/source IP as seen by the finance application.",
                "# This is NOT your dynamic public IP or dynamic-DNS hostname.",
                "# A different subnet across a Layer 3 router is fine if routable.",
                "# If source NAT intervenes, use the address the app sees, provided it",
                "# identifies only trusted proxy traffic. Keep that address stable.",
                "# Separate exact IPv4/IPv6 addresses with commas; no hostnames/subnets.",
            ])
        if key == "smtpPort":
            lines.extend(["# Omit port to use 587 for starttls or 465 for tls (TLS-on-connect).", "# smtpPort ="])
            continue
        if key == "smtpSecurity":
            lines.append("# Use starttls or tls. Encryption and certificate checks are required.")
        if key == "smtpTimeoutSeconds":
            lines.append("# Timeout per blocking network operation, not the total email attempt.")
        if key == "requireRegistrationVerification":
            lines.append("# true requires enforceEmailUsernames and smtpEnabled with valid settings.")
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        lines.append(f"{key} = {rendered}".rstrip())
    lines.extend(["", extraConfigurationHeader, extraConfigurationDescription])
    return "\n".join(lines) + "\n"


def loadConfiguration(directory):
    configuration = dict(configurationDefaults)
    suppliedValues = {}
    path = Path(directory) / "platform.conf"
    try:
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except (OSError, UnicodeError):
        raise ConfigurationError("Cannot read platform.conf. Check its permissions and UTF-8 encoding.") from None
    for lineNumber, line in enumerate(lines, 1):
        line = line.strip()
        if line == extraConfigurationHeader:
            break
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            if line.split()[0] in configurationDefaults:
                raise ConfigurationError(f"Invalid platform.conf syntax on line {lineNumber}. Use key = value.")
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if key not in configurationDefaults:
            continue
        suppliedValues[key] = value
    for key, value in suppliedValues.items():
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ConfigurationError(f"Invalid setting: {key}. Control characters are not allowed.")
        if isinstance(configurationDefaults[key], bool):
            if value.lower() not in {"true", "false"}:
                raise ConfigurationError(f"Invalid setting: {key}. Use true or false.")
            value = value.lower() == "true"
        elif key in {"smtpPort", "smtpTimeoutSeconds"}:
            if not value.isascii() or not value.isdecimal() or len(value) > 9:
                raise ConfigurationError(f"Invalid setting: {key}. Use a positive whole number.")
            value = int(value)
            if value < 1 or (key == "smtpPort" and value > 65535):
                raise ConfigurationError(f"Invalid setting: {key}. Check its allowed range.")
        configuration[key] = value
    if configuration["smtpSecurity"] not in {"starttls", "tls"}:
        raise ConfigurationError("Invalid setting: smtpSecurity. Use starttls or tls.")
    addresses = configuration["trustedProxyAddresses"]
    try:
        configuration["trustedProxyAddresses"] = frozenset(str(ipaddress.ip_address(address.strip())) for address in addresses.split(",")) if addresses else frozenset()
    except ValueError:
        raise ConfigurationError("Invalid setting: trustedProxyAddresses. Use exact IPv4/IPv6 addresses separated by commas.") from None
    configuration["smtpPort"] = configuration["smtpPort"] or (587 if configuration["smtpSecurity"] == "starttls" else 465)
    if bool(configuration["smtpUsername"]) != bool(configuration["smtpPassword"]):
        raise ConfigurationError("SMTP authentication requires both smtpUsername and smtpPassword, or neither.")
    if configuration["smtpEnabled"]:
        sender = configuration["smtpFromAddress"]
        if not configuration["smtpHost"] or any(character.isspace() for character in configuration["smtpHost"]):
            raise ConfigurationError("Enabled SMTP requires a valid smtpHost.")
        if sender.count("@") != 1 or any(character.isspace() for character in sender) or not all(sender.split("@")):
            raise ConfigurationError("Enabled SMTP requires a valid smtpFromAddress.")
    if configuration["requireRegistrationVerification"] and not (configuration["smtpEnabled"] and configuration["enforceEmailUsernames"]):
        raise ConfigurationError("Required verification needs enforceEmailUsernames and smtpEnabled. Enable both with valid SMTP settings, or explicitly disable requireRegistrationVerification.")
    return configuration


def updateConfigurationFile(path, content, original):
    """Publish complete files while retaining private-file permissions."""
    if path.is_symlink():
        raise OSError("Configuration links are not rewritten")
    if content == original:
        return
    descriptor, temporaryName = tempfile.mkstemp(prefix=f".{path.name}-", suffix=".tmp", dir=path.parent)
    temporaryPath = Path(temporaryName)
    os.close(descriptor)
    try:
        if original is not None:
            if os.name == "nt":
                # Protect the staging file with the original DACL before writing
                # secrets. ReplaceFile retains the original access rules afterward.
                import ctypes
                from ctypes import wintypes
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                security = ctypes.WinDLL("advapi32", use_last_error=True)
                getSecurity = security.GetFileSecurityW
                getSecurity.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
                getSecurity.restype = wintypes.BOOL
                required = wintypes.DWORD()
                getSecurity(str(path), 4, None, 0, ctypes.byref(required))
                if not required.value:
                    raise ctypes.WinError(ctypes.get_last_error())
                descriptor = ctypes.create_string_buffer(required.value)
                if not getSecurity(str(path), 4, descriptor, required.value, ctypes.byref(required)):
                    raise ctypes.WinError(ctypes.get_last_error())
                setSecurity = security.SetFileSecurityW
                setSecurity.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
                setSecurity.restype = wintypes.BOOL
                # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION:
                # the staging directory must not add broader inherited access.
                if not setSecurity(str(temporaryPath), 4 | 0x80000000, descriptor):
                    raise ctypes.WinError(ctypes.get_last_error())
            else:
                details = path.stat()
                stagingDetails = temporaryPath.stat()
                if (details.st_uid, details.st_gid) != (stagingDetails.st_uid, stagingDetails.st_gid):
                    os.chown(temporaryPath, details.st_uid, details.st_gid)
                shutil.copystat(path, temporaryPath)
        with temporaryPath.open("wb") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        # Do not knowingly overwrite an administrator's edit during startup.
        current = path.read_bytes() if path.exists() else None
        if current != original:
            raise OSError("Configuration changed during startup")
        if os.name == "nt" and original is not None:
            replaceFile = kernel.ReplaceFileW
            replaceFile.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
            replaceFile.restype = wintypes.BOOL
            if not replaceFile(str(path), str(temporaryPath), None, 0, None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            os.replace(temporaryPath, path)
    finally:
        temporaryPath.unlink(missing_ok=True)


def synchronizeConfigurationFiles(directory, operationalLogs):
    """Refresh documentation only after the existing settings pass validation."""
    directory = Path(directory)
    sample = configurationSample()
    for filename in ("platform.conf.sample", "platform.conf"):
        path = directory / filename
        try:
            if path.is_symlink():
                raise OSError("Configuration links are not rewritten")
            original = path.read_bytes() if path.exists() else None
            if original is None and filename == "platform.conf":
                continue
            content = sample
            if filename == "platform.conf":
                overrides = {}
                notes = []
                extraLines = []
                sampleComments = {line.strip() for line in sample.splitlines() if line.startswith("#")}
                originalLines = original.decode("utf-8").splitlines()
                for index, line in enumerate(originalLines):
                    stripped = line.strip()
                    if stripped == extraConfigurationHeader:
                        remaining = originalLines[index + 1:]
                        if remaining and remaining[0] == extraConfigurationDescription:
                            remaining = remaining[1:]
                        extraLines.extend(remaining)
                        break
                    assignment = stripped.removeprefix("#").strip() if stripped.startswith("#") else stripped
                    key = assignment.split("=", 1)[0].strip()
                    if "=" in assignment and key in configurationDefaults:
                        # Prefer an explicit assignment over a commented example.
                        if key in overrides and stripped.startswith("#"):
                            notes.append(line)
                        else:
                            if key in overrides:
                                previous = overrides[key]
                                notes.append(previous if previous.lstrip().startswith("#") else f"# Earlier duplicate (inactive): {previous}")
                            overrides[key] = line
                    elif stripped.startswith("#") and stripped not in sampleComments and stripped != "# Preserved notes from your existing file":
                        notes.append(line)
                    elif stripped and not stripped.startswith("#"):
                        extraLines.append(line)
                lines = []
                for line in sample.partition(extraConfigurationHeader)[0].rstrip().splitlines():
                    assignment = line.removeprefix("#").strip()
                    key = assignment.split("=", 1)[0].strip()
                    lines.append(overrides.get(key, line) if "=" in assignment else line)
                if notes:
                    lines.extend(["", "# Preserved notes from your existing file", *notes])
                lines.extend(["", extraConfigurationHeader, extraConfigurationDescription, *extraLines])
                content = "\n".join(lines) + "\n"
            updateConfigurationFile(path, content.encode("utf-8"), original)
            if content.encode("utf-8") != original:
                operationalLogs.write("startup", f"Updated {filename}. Existing configured values were preserved; the sample contains defaults only.")
        except (OSError, UnicodeError):
            operationalLogs.write("startup", f"Could not update {filename}. Check file and folder permissions. The application will continue using its validated settings.")


class TextLogHandler(RotatingFileHandler):
    def emit(self, record):
        try:
            super().emit(record)
        finally:
            # Close between writes so updates/backups and temporary tests do not
            # retain open handles; rotation still runs under the handler lock.
            if self.stream is not None:
                self.stream.close()
                self.stream = None


class OperationalLogs:
    """Only fixed messages and safe identities are sent to these files."""

    def __init__(self, directory):
        directory = Path(directory) / "logs"
        directory.mkdir(parents=True, exist_ok=True)
        self.loggers = {}
        self.warned = set()
        self.lock = threading.Lock()
        for name in ("startup", "email", "userManagement"):
            logger = logging.Logger(f"npf.{name}.{id(self)}", level=logging.INFO)
            handler = TextLogHandler(directory / f"{name}.log", maxBytes=1024 * 1024, backupCount=3, encoding="utf-8", delay=True)
            handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S %z"))
            logger.addHandler(handler)
            self.loggers[name] = logger

    def write(self, name, message):
        try:
            self.loggers[name].info(message)
        except OSError:
            logging.error("Operational log could not be written. Check the logs folder permissions.")

    def warnOnce(self, condition, message):
        with self.lock:
            if condition in self.warned:
                return
            self.warned.add(condition)
        self.write("startup", message)


class TrustedProxy:
    """Resolve before Flask constructs its request; trust the original socket peer."""

    def __init__(self, application, addresses, logs):
        self.application, self.addresses, self.logs = application, addresses, logs

    def __call__(self, environment, startResponse):
        try:
            peer = str(ipaddress.ip_address(environment.get("REMOTE_ADDR", "")))
        except ValueError:
            peer = environment.get("REMOTE_ADDR", "")
        environment["npf.directPeer"] = peer
        if peer in self.addresses:
            for header, target in (("HTTP_X_FORWARDED_FOR", "REMOTE_ADDR"), ("HTTP_X_FORWARDED_PROTO", "wsgi.url_scheme")):
                supplied = environment.get(header)
                if supplied is None:
                    fieldName = "X-Forwarded-For" if target == "REMOTE_ADDR" else "X-Forwarded-Proto"
                    consequence = "Visitors may share the proxy's sign-in limits." if target == "REMOTE_ADDR" else "Cookies may lack Secure when the direct connection uses HTTP."
                    self.logs.warnOnce(header, f"Proxy setup is not recommended: {fieldName} is missing. Check NGINX forwarding settings. {consequence}")
                    continue
                value = supplied.split(",")[-1].strip()
                try:
                    if target == "REMOTE_ADDR":
                        value = str(ipaddress.ip_address(value))
                    elif value.lower() in {"http", "https"}:
                        value = value.lower()
                    else:
                        raise ValueError()
                except ValueError:
                    startResponse("400 Bad Request", [("Content-Type", "text/plain; charset=utf-8"), ("Cache-Control", "no-store")])
                    return [b"Invalid proxy forwarding header. Check the NGINX forwarding settings."]
                environment[target] = value
        return self.application(environment, startResponse)
