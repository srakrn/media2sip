# Documentation

Two tracks. Pick the one that matches what you are doing.

## Using it — [`user/`](user/)

For running media2sip on your own Home Assistant and PBX.

| | |
| --- | --- |
| [installation.md](user/installation.md) | create the extension, run the sidecar, add the integration |
| [configuration.md](user/configuration.md) | every setting on both halves, in one place |
| [networking.md](user/networking.md) | host vs bridged, and why SIP cares |
| [usage.md](user/usage.md) | the entity, the services, and what happens when two pages collide |
| [troubleshooting.md](user/troubleshooting.md) | when a page does not arrive |

## Working on it — [`dev/`](dev/)

For changing the code, running the suites, or cutting a release.

| | |
| --- | --- |
| [architecture.md](dev/architecture.md) | how the two halves are split, and why |
| [testing.md](dev/testing.md) | the three test suites and their traps |
| [releasing.md](dev/releasing.md) | how the integration and the image stay in step |

The sidecar's own internals — control API, media pipeline, threading — are in
[`sidecar/README.md`](../sidecar/README.md).
