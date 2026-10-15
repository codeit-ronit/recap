# ShopSim architecture

ShopSim is the small online shop that RECAP V1 investigates. It is a simulated local
sandbox, not a real cluster: five services run in one namespace, and every signal they
emit is generated deterministically by the simulator.

## Services

- frontend: serves pages and the public API to shoppers.
- checkout: turns a cart into an order and coordinates payment and stock.
- payment: authorizes and captures card payments.
- inventory: reserves and releases stock for ordered items.
- cart: stores the items a shopper has selected.

Services normally run two or more replicas, so a single unhealthy pod does not take a
whole service down.

## Call graph

- frontend calls checkout and cart.
- checkout calls payment and inventory.
- payment, inventory and cart call no other service.

A service that another service calls is its dependency; the service making the call is
the caller. Requests therefore flow from frontend to checkout, and from checkout to
payment and inventory.

## Service level objectives

- Errors: the HTTP 5xx ratio of every service stays under 1% of requests.
- Latency: p95 latency stays under 500 ms for every service.
- Availability: all replicas of every service report ready.

An alert fires when a service breaks one of these objectives. The alert names the
service whose numbers crossed the line, which is not always the service that is broken.

## How failures propagate

When a dependency fails, its callers fail too. A caller that cannot get answers from a
dependency logs errors of the form `upstream <service> call failed: <reason>` and
returns 5xx responses itself, so its error rate and latency rise. The caller's own pods
stay healthy: they are Running and Ready, their restart counts stay flat, and their
memory looks normal.

Propagation is transitive. When a service deep in the call graph fails, every service
above it on the path can report upstream errors. The deepest service whose errors are
its own, rather than upstream errors, is where the root cause usually lives.
