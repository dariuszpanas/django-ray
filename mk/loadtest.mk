# Load testing with Locust
# Include in main Makefile with: include mk/loadtest.mk

.PHONY: loadtest loadtest-demo loadtest-headless loadtest-18 loadtest-quick loadtest-moderate loadtest-stress

# Default host for the local KubeRay application endpoint
LOADTEST_HOST ?= http://localhost:30080
LOADTEST_USERS ?= 1
LOADTEST_SPAWN_RATE ?= 1
LOADTEST_DURATION ?= 300s
# Enqueue + the longest poll + final detail read, with scheduling margin.
LOADTEST_STOP_TIMEOUT ?= 150
LOADTEST_CLASSES ?= ObservabilityDemoUser

# Run Locust with web UI (http://localhost:8089)
loadtest:
	uv run locust -f locustfile.py --host=$(LOADTEST_HOST) -u $(LOADTEST_USERS) -r $(LOADTEST_SPAWN_RATE) --stop-timeout $(LOADTEST_STOP_TIMEOUT) $(LOADTEST_CLASSES)

# Resource-bounded deterministic demo for logs, admin, and the Ray dashboard
loadtest-demo:
	uv run locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u 1 -r 1 -t 300s --stop-timeout $(LOADTEST_STOP_TIMEOUT) --require-complete-tour ObservabilityDemoUser

# Generic headless load test; defaults to the one-user observability demo
loadtest-headless:
	uv run locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u $(LOADTEST_USERS) -r $(LOADTEST_SPAWN_RATE) -t $(LOADTEST_DURATION) --stop-timeout $(LOADTEST_STOP_TIMEOUT) $(LOADTEST_CLASSES)

# Explicit heavier historical baseline; not intended for constrained laptops
loadtest-18:
	uv run locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u 18 -r 3 -t 300s BasicTaskUser LocalRayUser MonitoringUser

# Short basic enqueue-capacity sample
loadtest-quick:
	uv run locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u 3 -r 1 -t 60s BasicTaskUser

# Moderate sustained enqueue-capacity sample
loadtest-moderate:
	uv run locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u 10 -r 2 -t 120s SustainedLoadUser

# Explicit stress workload - USE WITH CAUTION
loadtest-stress:
	uv run locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u 20 -r 5 -t 60s StressTestUser

