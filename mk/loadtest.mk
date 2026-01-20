# Load testing with Locust
# Include in main Makefile with: include mk/loadtest.mk

.PHONY: loadtest loadtest-quick loadtest-moderate loadtest-stress

# Default host for load testing (NodePort access)
LOADTEST_HOST ?= http://localhost:30080

# Run Locust with web UI (http://localhost:8089)
loadtest:
	locust -f locustfile.py --host=$(LOADTEST_HOST)

# Quick load test (100 users, 60 seconds)
loadtest-quick:
	locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u 100 -r 10 -t 60s

# Moderate load test (50 users, 2 minutes)
loadtest-moderate:
	locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u 50 -r 5 -t 120s

# Stress test (200 users, 60 seconds) - USE WITH CAUTION
loadtest-stress:
	locust -f locustfile.py --host=$(LOADTEST_HOST) --headless -u 200 -r 50 -t 60s

