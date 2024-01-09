<!-- markdownlint-disable -->

<a href="../src/charm_state.py#L0"><img align="right" style="float:right;" src="https://img.shields.io/badge/-source-cccccc?style=flat-square"></a>

# <kbd>module</kbd> `charm_state.py`
The module for managing charm state.

**Global Variables**
---------------
- **AGENT_RELATION**


---

## <kbd>class</kbd> `AgentMeta`
The Jenkins agent metadata.

Attrs:  executors: The number of executors available on the unit.  labels: The comma separated labels to assign to the agent.  name: The name of the agent.




---

<a href="../src/charm_state.py#L48"><img align="right" style="float:right;" src="https://img.shields.io/badge/-source-cccccc?style=flat-square"></a>

### <kbd>function</kbd> `as_dict`

```python
as_dict() → Dict[str, str]
```

Return dictionary representation of agent metadata.



**Returns:**
  A dictionary adhering to jenkins_agent_v0 interface.


---

## <kbd>class</kbd> `Credentials`
The credentials used to register to the Jenkins server.

Attrs:  address: The Jenkins server address to register to.  secret: The secret used to register agent.





---

## <kbd>class</kbd> `InvalidStateError`
Exception raised when state configuration is invalid.

<a href="../src/charm_state.py#L74"><img align="right" style="float:right;" src="https://img.shields.io/badge/-source-cccccc?style=flat-square"></a>

### <kbd>function</kbd> `__init__`

```python
__init__(msg: str = '')
```

Initialize a new instance of the InvalidStateError exception.



**Args:**

 - <b>`msg`</b>:  Explanation of the error.





---

## <kbd>class</kbd> `State`
The Jenkins agent state.

Attrs:  agent_meta: The Jenkins agent metadata to register on Jenkins server.  agent_relation_credentials: The full set of credentials from the agent relation. None if  partial data is set or the credentials do not belong to current agent.  unit_data: Data about the current unit.  jenkins_agent_service_name: The Jenkins agent workload container name.




---

<a href="../src/charm_state.py#L135"><img align="right" style="float:right;" src="https://img.shields.io/badge/-source-cccccc?style=flat-square"></a>

### <kbd>classmethod</kbd> `from_charm`

```python
from_charm(charm: CharmBase) → State
```

Initialize the state from charm.



**Args:**

 - <b>`charm`</b>:  The root Jenkins agent charm.



**Raises:**

 - <b>`InvalidStateError`</b>:  if invalid state values were encountered.



**Returns:**
 Current state of Jenkins agent.


---

## <kbd>class</kbd> `UnitData`
The charm's unit data.

Attrs:  series: The base of the machine on which the charm is running.
