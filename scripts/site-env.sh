# Sourced by the shell tools. Loads site settings from $SMALLTV_ENV or <repo>/.env with the same rules
# as smalltv_monitor/siteenv.py: KEY=value lines, '#' comment lines, quotes stripped, parsed (never
# executed), values already in the environment win, empty means unset.
load_site_env() {
    local file="${SMALLTV_ENV:-$1/.env}" line key value
    [[ -f "$file" ]] || return 0
    while IFS= read -r line || [[ -n "$line" ]]; do
        [[ "$line" =~ ^[[:space:]]*(export[[:space:]]+)?([A-Z_][A-Z0-9_]*)[[:space:]]*=(.*)$ ]] || continue
        key="${BASH_REMATCH[2]}"
        value="${BASH_REMATCH[3]}"
        value="${value#"${value%%[![:space:]]*}"}"
        value="${value%"${value##*[![:space:]]}"}"
        if [[ ${#value} -ge 2 && ( "$value" == \"*\" || "$value" == \'*\' ) ]]; then
            value="${value:1:${#value}-2}"
        fi
        [[ -n "${!key:-}" || -z "$value" ]] || export "$key=$value"
    done <"$file"
}
