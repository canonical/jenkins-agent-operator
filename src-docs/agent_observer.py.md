<!-- markdownlint-disable -->

<a href="../src/agent_observer.py#L0"><img align="right" style="float:right;" src="https://img.shields.io/badge/-source-cccccc?style=flat-square"></a>

# <kbd>module</kbd> `agent_observer.py`
The agent relation observer module.

**Global Variables**
---------------
- **AGENT_RELATION**


---

## <kbd>class</kbd> `Observer`
The Jenkins agent relation observer.

<a href="../src/agent_observer.py#L20"><img align="right" style="float:right;" src="https://img.shields.io/badge/-source-cccccc?style=flat-square"></a>

### <kbd>function</kbd> `__init__`

```python
__init__(
    charm: CharmBase,
    state: State,
    jenkins_agent_service: JenkinsAgentService
)
```

Initialize the observer and register event handlers.



**Args:**

 - <b>`charm`</b>:  The parent charm to attach the observer to.
 - <b>`state`</b>:  The charm state.
 - <b>`jenkins_agent_service`</b>:  Service manager that controls Jenkins agent service.


---

#### <kbd>property</kbd> model

Shortcut for more simple access the model.
