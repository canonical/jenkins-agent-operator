<!-- markdownlint-disable -->

<a href="../src/service.py#L0"><img align="right" style="float:right;" src="https://img.shields.io/badge/-source-cccccc?style=flat-square"></a>

# <kbd>module</kbd> `service.py`
The agent pebble service module.

**Global Variables**
---------------
- **AGENT_SERVICE_NAME**
- **AGENT_PACKAGE_NAME**
- **SYSTEMD_SERVICE_CONF_DIR**
- **PPA_URI**
- **PPA_DEB_SRC**
- **PPA_GPG_KEY_ID**
- **STARTUP_CHECK_TIMEOUT**
- **STARTUP_CHECK_INTERVAL**


---

## <kbd>class</kbd> `FileRenderError`
Exception raised when failing to interact with a file in the filesystem.





---

## <kbd>class</kbd> `JenkinsAgentService`
Jenkins agent service class.

Attrs:  is_active: Indicate if the agent service is active and running.

<a href="../src/service.py#L54"><img align="right" style="float:right;" src="https://img.shields.io/badge/-source-cccccc?style=flat-square"></a>

### <kbd>function</kbd> `__init__`

```python
__init__(state: State)
```

Initialize the jenkins agent service.



**Args:**

 - <b>`state`</b>:  The Jenkins agent state.


---

#### <kbd>property</kbd> is_active

Indicate if the jenkins agent service is active.



---

<a href="../src/service.py#L95"><img align="right" style="float:right;" src="https://img.shields.io/badge/-source-cccccc?style=flat-square"></a>

### <kbd>function</kbd> `install`

```python
install() → None
```

Install and set up the jenkins agent apt package.



**Raises:**

 - <b>`PackageInstallError`</b>:  if the package installation failed.

---

<a href="../src/service.py#L123"><img align="right" style="float:right;" src="https://img.shields.io/badge/-source-cccccc?style=flat-square"></a>

### <kbd>function</kbd> `restart`

```python
restart() → None
```

Start the agent service.



**Raises:**

 - <b>`ServiceRestartError`</b>:  when restarting the service fails

---

<a href="../src/service.py#L166"><img align="right" style="float:right;" src="https://img.shields.io/badge/-source-cccccc?style=flat-square"></a>

### <kbd>function</kbd> `stop`

```python
stop() → None
```

Stop the agent service.



**Raises:**

 - <b>`ServiceStopError`</b>:  if systemctl stop returns a non-zero exit code.


---

## <kbd>class</kbd> `PackageInstallError`
Exception raised when package installation fails.





---

## <kbd>class</kbd> `ServiceRestartError`
Exception raised when failing to start the agent service.





---

## <kbd>class</kbd> `ServiceStopError`
Exception raised when failing to stop the agent service.
