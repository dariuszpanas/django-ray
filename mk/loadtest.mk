# Load testing with Locust
# Include in main Makefile with: include mk/loadtest.mk

.PHONY: loadtest loadtest-headless loadtest-18 loadtest-quick loadtest-moderate loadtest-stress

# Default host for load testing (Kong local overlay path)
LOADTEST_HOST ?= http://localhost:30080
LOADTEST_USERS ?= 18
LOADTEST_SPAWN_RATE ?= 3
LOADTEST_DURATION ?= 300s
LOADTEST_CLASSES ?= BasicTaskUser LocalRayUser MonitoringUser

# Run Locust with web UI (http://localhost:8089)
loadtest:
	uv run locust -f locustfile.py --host=$(LOADTEST_HOST)

# Generic headless load test
loadtest-headless:
	uv run locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u $(LOADTEST_USERS) -r $(LOADTEST_SPAWN_RATE) -t $(LOADTEST_DURATION) $(LOADTEST_CLASSES)

# Validated sustained mixed-load baseline for the current local stack
loadtest-18:
	uv run locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u 18 -r 3 -t 300s BasicTaskUser LocalRayUser MonitoringUser

# Quick load test (100 users, 60 seconds)
loadtest-quick:
	uv run locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u 100 -r 10 -t 60s

# Moderate load test (50 users, 2 minutes)
loadtest-moderate:
	uv run locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u 50 -r 5 -t 120s

# Stress test (200 users, 60 seconds) - USE WITH CAUTION
loadtest-stress:
	uv run locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u 200 -r 50 -t 60s

