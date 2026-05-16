# Helm chart developer tasks. Run from infrastructure/helm/.

CHARTS_LIB      := kanto-common
CHARTS_APP      := cluster-bootstrap
SERVICE_CHARTS  := $(wildcard ../../services/*/helm)

# K8s API version used for kubeconform validation. Match the AKS
# version in infrastructure/terraform/config/<env>.tfvars.
KUBE_VERSION    ?= 1.34.0

# CRD catalog fallback (SecretProviderClass etc).
CRD_SCHEMAS := 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'

.PHONY: help lint template validate test ci deps

help:
	@echo "Targets:"
	@echo "  deps      vendor library chart into every dependent chart"
	@echo "  lint      helm lint every chart"
	@echo "  template  helm template every chart with dev + prod values"
	@echo "  validate  kubeconform on rendered output"
	@echo "  test      helm unittest every chart with tests/"
	@echo "  ci        lint + template + validate + test"

deps:
	@set -e ; \
	for d in $(CHARTS_LIB)/tests/fixtures/consumer-chart $(CHARTS_APP) $(SERVICE_CHARTS) ; do \
	  if [ -f $$d/Chart.yaml ] && grep -q '^dependencies:' $$d/Chart.yaml ; then \
	    echo "==> dep update: $$d" ; \
	    helm dependency update $$d ; \
	  fi ; \
	done

lint: deps
	@set -e ; \
	for d in $(CHARTS_LIB) $(CHARTS_APP) $(SERVICE_CHARTS) ; do \
	  if [ -f $$d/Chart.yaml ]; then \
	    echo "==> lint: $$d" ; \
	    helm lint $$d ; \
	  fi ; \
	done

# ``basename`` collides for every services/*/helm directory, so we
# read the canonical chart name out of each Chart.yaml and use that
# as the rendered file's stem. That keeps each chart's manifests in
# its own .rendered/<chart-name>-<env>.yaml.
template: deps
	@set -e ; \
	mkdir -p .rendered ; \
	for d in $(CHARTS_APP) $(SERVICE_CHARTS) ; do \
	  if [ -f $$d/Chart.yaml ]; then \
	    name=$$(awk '/^name:/ { print $$2; exit }' $$d/Chart.yaml) ; \
	    for env in dev prod ; do \
	      if [ -f $$d/values-$$env.yaml ]; then \
	        echo "==> template: $$d ($$env)" ; \
	        helm template render $$d -f $$d/values-$$env.yaml \
	          > .rendered/$$name-$$env.yaml ; \
	      fi ; \
	    done ; \
	  fi ; \
	done ; \
	echo "==> template: $(CHARTS_LIB)/tests/fixtures/consumer-chart" ; \
	helm template render $(CHARTS_LIB)/tests/fixtures/consumer-chart \
	  > .rendered/consumer-chart.yaml

validate: template
	@set -e ; \
	for f in .rendered/*.yaml ; do \
	  echo "==> kubeconform: $$f" ; \
	  kubeconform -kubernetes-version $(KUBE_VERSION) -strict -summary \
	    -schema-location default \
	    -schema-location $(CRD_SCHEMAS) \
	    $$f ; \
	done

test: deps
	@set -e ; \
	for d in $(CHARTS_LIB)/tests/fixtures/consumer-chart $(CHARTS_APP) $(SERVICE_CHARTS) ; do \
	  if [ -d $$d/tests ] && ls $$d/tests/*_test.yaml >/dev/null 2>&1 ; then \
	    echo "==> unittest: $$d" ; \
	    helm unittest $$d ; \
	  fi ; \
	done

ci: lint validate test
	@echo "==> ci ok"

clean:
	rm -rf .rendered
	find . -name 'charts' -type d -exec rm -rf {} +
	find . -name 'Chart.lock' -delete
