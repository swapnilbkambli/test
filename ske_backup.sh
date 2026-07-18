#!/usr/bin/env bash
# ==============================================================================
# ske_backup.sh  —  Kubernetes deployment backup via skectl + helm + kubectl
#
# Usage  : bash ske_backup.sh
# Requires: skectl, helm, kubectl in PATH (or override via env vars below)
# Works in: MobaXterm local terminal (bash 4+, no jq required)
# ==============================================================================

# ─── ANSI colours (auto-disabled if output is not a terminal) ─────────────────
if [[ -t 1 ]]; then
    cR='\033[0;31m' cG='\033[0;32m' cY='\033[1;33m'
    cB='\033[0;34m' cC='\033[0;36m' cM='\033[0;35m'
    cW='\033[1m'    cZ='\033[0m'
else
    cR='' cG='' cY='' cB='' cC='' cM='' cW='' cZ=''
fi

# Execution log — starts in /tmp, moved into the backup dir once it's created
_LOG_TMP=$(mktemp 2>/dev/null) || _LOG_TMP="/tmp/ske_backup_$$.log"
LOG_FILE="$_LOG_TMP"
_log()     { printf "[%s] %s\n" "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG_FILE"; }

_info()    { printf "  ${cC}▸${cZ} %s\n"      "$*"; _log "INFO    $*"; }
_ok()      { printf "  ${cG}✓${cZ} %s\n"      "$*"; _log "OK      $*"; }
_warn()    { printf "  ${cY}⚠${cZ} %s\n"      "$*" >&2; _log "WARN    $*"; }
_die()     { printf "\n  ${cR}✗ %s${cZ}\n\n"  "$*" >&2; _log "ERROR   $*"; exit 1; }
_section() { printf "\n${cW}${cB}━━━  %s  ━━━${cZ}\n" "$*"; _log ""; _log "=== $* ==="; }

# ─── TOOL PATHS  (override with env vars if tools are not in PATH) ────────────
SKECTL="${SKECTL_CMD:-skectl}"
KUBECTL="${KUBECTL_CMD:-kubectl}"
HELM="${HELM_CMD:-helm}"

# ==============================================================================
#  CONFIGURATION — fill in your actual URLs before first use
# ==============================================================================

declare -A ENV_SKE_URL=(
    [DF_1]="https://ske.df1.example.com"
    [DF_2]="https://ske.df2.example.com"
    [Prod]="https://ske.prod.example.com"
    [DR]="https://ske.dr.example.com"
)
declare -A ENV_AUTH_URL=(
    [DF_1]="https://auth.df1.example.com"
    [DF_2]="https://auth.df2.example.com"
    [Prod]="https://auth.prod.example.com"
    [DR]="https://auth.dr.example.com"
)
# Pre-fill username per env — leave blank to always prompt
declare -A ENV_DEFAULT_USER=(
    [DF_1]=""
    [DF_2]=""
    [Prod]=""
    [DR]=""
)
# Suggested default namespaces per env (comma-separated) — user can override at runtime
declare -A ENV_DEFAULT_NS=(
    [DF_1]="t-55547-df1"
    [DF_2]="t-55547-df2"
    [Prod]="t-55547-prod"
    [DR]="t-55547-dr"
)

# Order shown in the environment menu
ENV_ORDER=(DF_1 DF_2 Prod DR)

# ==============================================================================
#  COMPONENT CATALOG
# ==============================================================================

COMP_KINDS=(
    Deployment
    StatefulSet
    Service
    ConfigMap
    Secret
    Ingress
    HorizontalPodAutoscaler
    PersistentVolumeClaim
    CronJob
    ServiceAccount
    Role
    RoleBinding
    PodDisruptionBudget
    NetworkPolicy
)
COMP_LABELS=(
    "Deployments"
    "StatefulSets"
    "Services"
    "ConfigMaps"
    "Secrets"
    "Ingresses"
    "HPAs  (HorizontalPodAutoscaler)"
    "PVCs  (PersistentVolumeClaim)"
    "CronJobs"
    "ServiceAccounts"
    "Roles"
    "RoleBindings"
    "PodDisruptionBudgets"
    "NetworkPolicies"
)

# ==============================================================================
#  HELPERS
# ==============================================================================

# Temp-file cleanup on exit
_TMPFILES=()
_cleanup() { rm -f "${_TMPFILES[@]}" 2>/dev/null; }
trap _cleanup EXIT

# Parse 'helm get manifest' output and emit one "Kind/name" line per resource.
# Pure awk — no jq/python needed.
_parse_manifest() {
    local release="$1" ns="$2"
    _log "CMD     $HELM get manifest $release -n $ns"
    "$HELM" get manifest "$release" -n "$ns" 2>/dev/null | awk '
        /^---/          { kind=""; in_meta=0 }
        /^kind:/        { kind=$2; gsub(/[^a-zA-Z]/, "", kind) }
        /^metadata:/    { in_meta=(kind != "") }
        in_meta && /^  name:/ {
            name = $2
            gsub(/[^a-zA-Z0-9._-]/, "", name)
            if (kind != "" && name != "") print kind "/" name
            in_meta = 0
        }
        /^[a-zA-Z]/ && !/^(kind|metadata):/ { in_meta = 0 }
    '
}

# Fetch one named resource as YAML to a file; return 1 if missing / empty.
_backup_resource() {
    local kind="$1" name="$2" ns="$3" outfile="$4"
    _log "CMD     $KUBECTL get $kind $name -n $ns -o yaml"
    "$KUBECTL" get "$kind" "$name" -n "$ns" -o yaml 2>/dev/null > "$outfile"
    [[ -s "$outfile" ]]
}

# ==============================================================================
#  STEP 1 — ENVIRONMENT
# ==============================================================================
_section "Environment Selection"
echo ""
for i in "${!ENV_ORDER[@]}"; do
    printf "    %d)  %s\n" $((i+1)) "${ENV_ORDER[$i]}"
done
echo ""
while true; do
    read -rp "  Select environment [1-${#ENV_ORDER[@]}]: " _pick
    [[ "$_pick" =~ ^[0-9]+$ ]] \
        && (( _pick >= 1 && _pick <= ${#ENV_ORDER[@]} )) \
        && break
    _warn "Please enter a number between 1 and ${#ENV_ORDER[@]}."
done
SELECTED_ENV="${ENV_ORDER[$(( _pick - 1 ))]}"
_ok "Environment : $SELECTED_ENV"

SKE_URL="${ENV_SKE_URL[$SELECTED_ENV]}"
AUTH_URL="${ENV_AUTH_URL[$SELECTED_ENV]}"

# ==============================================================================
#  STEP 2 — NAMESPACES
# ==============================================================================
_section "Namespace Selection"
echo ""
_default_ns="${ENV_DEFAULT_NS[$SELECTED_ENV]}"
_info "Default for $SELECTED_ENV : $_default_ns"
read -rp "  Namespace(s) [comma-separated, Enter for default]: " _ns_raw
_ns_raw="${_ns_raw:-$_default_ns}"
[[ -z "$_ns_raw" ]] && _die "At least one namespace is required."

IFS=',' read -ra NAMESPACES <<< "$_ns_raw"
for i in "${!NAMESPACES[@]}"; do
    NAMESPACES[$i]="${NAMESPACES[$i]//[[:space:]]/}"
done
_ok "Namespaces  : ${NAMESPACES[*]}"

# ==============================================================================
#  STEP 3 — LOGIN
# ==============================================================================
_section "Login  ($SELECTED_ENV)"
echo ""
_info "SKE URL  : $SKE_URL"
_info "Auth URL : $AUTH_URL"
echo ""

_def_user="${ENV_DEFAULT_USER[$SELECTED_ENV]}"
if [[ -n "$_def_user" ]]; then
    read -rp "  Username [$_def_user]: " _uname_in
    LOGIN_USER="${_uname_in:-$_def_user}"
else
    read -rp "  Username: " LOGIN_USER
fi
[[ -z "$LOGIN_USER" ]] && _die "Username cannot be empty."

read -srp "  Password: " LOGIN_PASS
echo ""
[[ -z "$LOGIN_PASS" ]] && _die "Password cannot be empty."

_info "Authenticating with skectl…"
_log "CMD     $SKECTL login $SKE_URL -s $AUTH_URL -u $LOGIN_USER -p ****"
_login_out=$("$SKECTL" login "$SKE_URL" -s "$AUTH_URL" -u "$LOGIN_USER" -p "$LOGIN_PASS" 2>&1)
_login_rc=$?
if [[ $_login_rc -ne 0 ]]; then
    _log "        skectl exit $_login_rc: $_login_out"
    _die "skectl login failed: ${_login_out}"
fi
_ok "Login successful."

_log "CMD     $KUBECTL config current-context"
KUBE_CTX=$("$KUBECTL" config current-context 2>/dev/null || printf "unknown")
_ok "kubectl context : $KUBE_CTX"

# ==============================================================================
#  STEP 4 — DISCOVER HELM RELEASES
# ==============================================================================
_section "Helm Release Discovery"
echo ""

ALL_RELEASES=()    # entries: "release-name|namespace"
ALL_REL_LINES=()   # raw helm list lines (for display)

for _ns in "${NAMESPACES[@]}"; do
    _info "Querying namespace: $_ns"
    _log "CMD     $HELM list -n $_ns --no-headers"
    while IFS= read -r _line; do
        [[ -z "$_line" ]] && continue
        _rname=$(awk '{print $1}' <<< "$_line")
        _rns=$(awk   '{print $2}' <<< "$_line")
        [[ -z "$_rname" || -z "$_rns" ]] && continue
        ALL_RELEASES+=("${_rname}|${_rns}")
        ALL_REL_LINES+=("$_line")
    done < <("$HELM" list -n "$_ns" --no-headers 2>/dev/null)
done

[[ ${#ALL_RELEASES[@]} -eq 0 ]] && _die "No helm releases found in: ${NAMESPACES[*]}"

echo ""
printf "  ${cW}%-5s${cZ} %s\n" "#" "$(printf '%-35s %-22s %s' 'Release' 'Namespace' 'Chart / Status')"
printf "  %s\n" "──────────────────────────────────────────────────────────────────────────"
for i in "${!ALL_RELEASES[@]}"; do
    printf "  ${cW}%3d)${cZ}  %s\n" $((i+1)) "${ALL_REL_LINES[$i]}"
done

# ==============================================================================
#  STEP 5 — SELECT RELEASES
# ==============================================================================
_section "Release Selection"
echo ""
echo "    all      →  back up every release listed above"
echo "    1        →  single release by number"
echo "    1,3,5    →  multiple releases by number"
echo ""
read -rp "  Your choice: " _rel_input
_rel_input="${_rel_input//[[:space:]]/}"
[[ -z "$_rel_input" ]] && _die "No selection entered."

CHOSEN_RELEASES=()
if [[ "$_rel_input" == "all" ]]; then
    CHOSEN_RELEASES=("${ALL_RELEASES[@]}")
else
    IFS=',' read -ra _picks <<< "$_rel_input"
    for _p in "${_picks[@]}"; do
        if [[ "$_p" =~ ^[0-9]+$ ]] && (( _p >= 1 && _p <= ${#ALL_RELEASES[@]} )); then
            CHOSEN_RELEASES+=("${ALL_RELEASES[$(( _p - 1 ))]}")
        else
            _warn "Ignoring invalid selection: $_p"
        fi
    done
fi
[[ ${#CHOSEN_RELEASES[@]} -eq 0 ]] && _die "No valid releases selected."

_ok "Selected ${#CHOSEN_RELEASES[@]} release(s):"
for _r in "${CHOSEN_RELEASES[@]}"; do
    IFS='|' read -r _rn _rns <<< "$_r"
    printf "      • %s  (ns: %s)\n" "$_rn" "$_rns"
done

# ==============================================================================
#  STEP 5b — PRE-FETCH HELM MANIFESTS
#  Done here so the component menu can show what actually exists, and so the
#  backup phase can reuse the results without a second helm round-trip.
# ==============================================================================
_section "Fetching Helm Manifests"
echo ""

declare -A REL_MANIFEST_FILE   # "release|ns" → path of cached Kind/name file
declare -A KIND_COUNTS          # Kind → total count across all chosen releases

for _rel_entry in "${CHOSEN_RELEASES[@]}"; do
    IFS='|' read -r _rel_name _rel_ns <<< "$_rel_entry"
    _info "Fetching: $_rel_name  (ns: $_rel_ns)"

    _mf=$(mktemp 2>/dev/null) || _mf="/tmp/ske_mf_${_rel_name}_$$"
    _TMPFILES+=("$_mf")
    _parse_manifest "$_rel_name" "$_rel_ns" > "$_mf"
    REL_MANIFEST_FILE["$_rel_entry"]="$_mf"

    _mf_count=$(wc -l < "$_mf" | tr -d '[:space:]')
    _ok "$_rel_name: $_mf_count resource(s) in manifest"
    _log "  Manifest $_rel_name (ns: $_rel_ns): $_mf_count resources"

    while IFS='/' read -r _k _n; do
        [[ -z "$_k" ]] && continue
        KIND_COUNTS["$_k"]=$(( ${KIND_COUNTS["$_k"]:-0} + 1 ))
    done < "$_mf"
done

# Log kind breakdown
_log "  Kind breakdown across chosen releases:"
for _k in "${COMP_KINDS[@]}"; do
    _c="${KIND_COUNTS[$_k]:-0}"
    [[ $_c -gt 0 ]] && _log "    $_k: $_c resource(s)"
done

# ==============================================================================
#  STEP 6 — SELECT COMPONENT TYPES
# ==============================================================================
_section "Component Selection"
echo ""
printf "  ${cW}%-5s %-40s %s${cZ}\n" "#" "Component type" "In selected releases"
printf "  %s\n" "──────────────────────────────────────────────────────────────────"
printf "  %-5s %-40s\n" "0)" "All components"
for i in "${!COMP_LABELS[@]}"; do
    _ck="${KIND_COUNTS[${COMP_KINDS[$i]}]:-0}"
    if [[ $_ck -gt 0 ]]; then
        printf "  ${cG}%-5d${cZ} %-40s ${cG}%d resource(s)${cZ}\n" \
            $((i+1)) "${COMP_LABELS[$i]}" "$_ck"
    else
        printf "  ${cY}%-5d${cZ} %-40s ${cY}(none)${cZ}\n" \
            $((i+1)) "${COMP_LABELS[$i]}"
    fi
done
echo ""
echo "  Enter 0 for all, or comma-separated numbers  (e.g. 1,4,5)"
read -rp "  Your choice: " _comp_input
_comp_input="${_comp_input//[[:space:]]/}"
[[ -z "$_comp_input" ]] && _die "No selection entered."

CHOSEN_KINDS=()
if [[ "$_comp_input" == "0" || "$_comp_input" == "all" ]]; then
    CHOSEN_KINDS=("${COMP_KINDS[@]}")
else
    IFS=',' read -ra _cpicks <<< "$_comp_input"
    for _p in "${_cpicks[@]}"; do
        if [[ "$_p" =~ ^[0-9]+$ ]] && (( _p >= 1 && _p <= ${#COMP_KINDS[@]} )); then
            CHOSEN_KINDS+=("${COMP_KINDS[$(( _p - 1 ))]}")
        else
            _warn "Ignoring invalid component number: $_p"
        fi
    done
fi
[[ ${#CHOSEN_KINDS[@]} -eq 0 ]] && _die "No valid component types selected."

# ==============================================================================
#  STEP 7 — CONFIRM
# ==============================================================================
_section "Confirm Backup"
echo ""
printf "  Environment  : ${cW}%s${cZ}\n"    "$SELECTED_ENV"
printf "  Namespaces   : %s\n"               "${NAMESPACES[*]}"
printf "  Releases     : %d selected\n"      "${#CHOSEN_RELEASES[@]}"
printf "  Components   : %s\n"               "${CHOSEN_KINDS[*]}"
printf "  Backup root  : %s\n"               "$(pwd)"
echo ""
read -rp "  Proceed? [y/N]: " _confirm
[[ "$_confirm" =~ ^[Yy]$ ]] || { echo "  Cancelled."; exit 0; }

# ==============================================================================
#  STEP 8 — EXECUTE BACKUP
# ==============================================================================
_section "Running Backup"
echo ""

BACKUP_TS=$(date +"%Y-%m-%d_%H%M%S")
BACKUP_ROOT="ske-backup_${SELECTED_ENV}_${BACKUP_TS}"
mkdir -p "$BACKUP_ROOT" || _die "Cannot create directory: $BACKUP_ROOT"

# Move pre-backup log into the backup directory now that it exists
LOG_FILE="$BACKUP_ROOT/execution_${BACKUP_TS}.log"
cp "$_LOG_TMP" "$LOG_FILE" 2>/dev/null
_TMPFILES+=("$_LOG_TMP")   # original temp cleaned up on exit
_log "=== Backup Execution Log ==="
_log "Script   : $0"
_log "Backup dir: $BACKUP_ROOT"

_ok "Backup directory: ${cW}$BACKUP_ROOT${cZ}"
_ok "Execution log   : ${cW}${LOG_FILE##*/}${cZ}"
echo ""

MANIFEST_FILE="$BACKUP_ROOT/backup-manifest.txt"
{
    printf "SKE Backup Manifest\n"
    printf "===================\n"
    printf "Date       : %s\n" "$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    printf "Environment: %s\n" "$SELECTED_ENV"
    printf "SKE URL    : %s\n" "$SKE_URL"
    printf "User       : %s\n" "$LOGIN_USER"
    printf "Context    : %s\n" "$KUBE_CTX"
    printf "Namespaces : %s\n" "${NAMESPACES[*]}"
    printf "Components : %s\n" "${CHOSEN_KINDS[*]}"
    printf "\nBacked up resources:\n"
} > "$MANIFEST_FILE"

_total_saved=0
_total_skip=0

for _rel_entry in "${CHOSEN_RELEASES[@]}"; do
    IFS='|' read -r _rel_name _rel_ns <<< "$_rel_entry"

    printf "  ${cM}●  Release: %s   (ns: %s)${cZ}\n" "$_rel_name" "$_rel_ns"
    printf "\n--- %s (ns: %s)\n" "$_rel_name" "$_rel_ns" >> "$MANIFEST_FILE"

    # Use pre-fetched manifest (no second helm round-trip)
    _mf="${REL_MANIFEST_FILE[$_rel_entry]}"

    _res_total=$(wc -l < "$_mf" | tr -d '[:space:]')
    if [[ "$_res_total" -eq 0 ]]; then
        _warn "Helm manifest empty for release: $_rel_name — skipping."
        echo "  (no resources found in helm manifest)" >> "$MANIFEST_FILE"
        continue
    fi
    _info "Helm manifest: $_res_total resource(s) under this release"

    for _kind in "${CHOSEN_KINDS[@]}"; do

        # Collect resource names of this kind from the cached manifest
        _names=()
        while IFS='/' read -r _k _n; do
            [[ "$_k" == "$_kind" ]] && _names+=("$_n")
        done < "$_mf"
        [[ ${#_names[@]} -eq 0 ]] && continue

        # Output dir: lowercase kind name
        _kdir=$(printf '%s' "$_kind" | tr '[:upper:]' '[:lower:]')
        _out_dir="$BACKUP_ROOT/$_rel_ns/$_rel_name/$_kdir"
        mkdir -p "$_out_dir"

        _saved=0
        _skip=0

        for _rname in "${_names[@]}"; do
            _outfile="$_out_dir/${_rname}.yaml"
            if _backup_resource "$_kind" "$_rname" "$_rel_ns" "$_outfile"; then
                _saved=$(( _saved + 1 ))
                _total_saved=$(( _total_saved + 1 ))
                printf "      %-30s %s\n" "$_kind/$_rname" "→ $_out_dir/${_rname}.yaml" >> "$MANIFEST_FILE"
                _log "  SAVE  $_rel_ns/$_rel_name  $_kind/$_rname"
            else
                _skip=$(( _skip + 1 ))
                _total_skip=$(( _total_skip + 1 ))
                rm -f "$_outfile"
                _log "  SKIP  $_rel_ns/$_rel_name  $_kind/$_rname  (not found in cluster)"
            fi
        done

        # Per-kind status line
        printf "    %-35s" "${_kind}:"
        [[ $_saved -gt 0 ]] && printf "${cG}%d saved${cZ}" "$_saved"
        [[ $_skip  -gt 0 ]] && printf "  ${cY}%d not found in cluster${cZ}" "$_skip"
        printf "\n"
    done
    echo ""
done

printf "\nTotal files saved : %d\nSkipped (not found): %d\n" \
    "$_total_saved" "$_total_skip" >> "$MANIFEST_FILE"
_log ""
_log "Total saved: $_total_saved   Skipped: $_total_skip"
_log "=== Backup complete ==="

# ==============================================================================
#  SUMMARY
# ==============================================================================
_section "Done"
echo ""
_ok "Backup directory : ${cW}$BACKUP_ROOT${cZ}"
_ok "Files saved      : ${cW}$_total_saved${cZ}"
[[ $_total_skip -gt 0 ]] && \
    _warn "Not found        : $_total_skip  (in helm manifest but absent from cluster)"
_ok "Manifest         : $MANIFEST_FILE"
echo ""
echo "  To restore a single resource:"
printf "  ${cC}kubectl apply -f %s/<namespace>/<release>/<kind>/<name>.yaml${cZ}\n" "$BACKUP_ROOT"
echo ""
echo "  To restore an entire release:"
printf "  ${cC}kubectl apply -R -f %s/<namespace>/<release>/${cZ}\n" "$BACKUP_ROOT"
echo ""
