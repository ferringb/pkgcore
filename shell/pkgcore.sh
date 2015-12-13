# Common library of useful shell functions leveraging pkgcore functionality.
# Source this file from your .bashrc, .zshrc, or similar.
#
# Note that most functions currently use non-POSIX features so bash or zsh are
# basically required.

# get an attribute for a given package
_pkgattr() {
	local pkg_attr=$1 pkg_atom=$2 repo=$3 p
	local -a pkg

	if [[ -z ${pkg_atom} ]]; then
		echo "Enter a valid package name." >&2
		return 1
	fi

	if [[ -n ${repo} ]]; then
		IFS=$'\n' pkg=( $(pquery -r "${repo}" --raw --unfiltered --cpv --one-attr "${pkg_attr}" -n -- "${pkg_atom}" 2>/dev/null) )
	else
		IFS=$'\n' pkg=( $(pquery --ebuild-repos --raw --unfiltered --cpv --one-attr "${pkg_attr}" -n -- "${pkg_atom}" 2>/dev/null) )
	fi
	if [[ $? != 0 ]]; then
		echo "Invalid package atom: '${pkg_atom}'" >&2
		return 1
	fi

	if [[ -z ${pkg[@]} ]]; then
		echo "No matches found." >&2
		return 1
	elif [[ ${#pkg[@]} > 1 ]]; then
		echo "Multiple matches found:" >&2
		for p in ${pkg[@]}; do
			echo ${p%:*} >&2
		done
		return 1
	fi
	echo ${pkg#*:}
}

# cross-shell compatible PATH searching
_which() {
	local shell=$(basename ${SHELL})
	if [[ ${shell} == "bash" ]]; then
		type -P "$1" >/dev/null
	elif [[ ${shell} == "zsh" ]]; then
		whence -p "$1" >/dev/null
	else
		which "$1" >/dev/null
	fi
	return $?
}

# change to a package directory
#
# usage: pcd pkg [repo]
# example: pcd sys-devel/gcc gentoo
#
# This will change the CWD to the sys-devel/gcc directory in the gentoo repo.
# Note that pkgcore's extended atom syntax is supported so one can also
# abbreviate the command to `pcd gcc gentoo` assuming there is only one package
# with a name of 'gcc' in the gentoo repo.
#
# Note that this should work for any local repo type on disk, e.g. one can also
# use this to enter the repos for installed or binpkgs via 'vdb' or 'binpkg'
# repo arguments, respectively.
pcd() {
	local pkgpath=$(_pkgattr path "$@")
	[[ -z ${pkgpath} ]] && return 1

	# find the nearest parent directory
	while [[ ! -d ${pkgpath} ]]; do
		pkgpath=$(dirname "${pkgpath}")
	done

	pushd "${pkgpath}" >/dev/null
}

# open a package's homepage in a browser
#
# usage: esite pkg [repo]
# example: esite sys-devel/gcc gentoo
#
# If a package has more than one homepage listed the first is selected.
#
# Note that this requires xdg-utils to be installed for xdg-open.
esite() {
	local homepage=$(_pkgattr homepage "$@")
	# only select the first homepage in a list
	homepage=${homepage%% *}
	[[ -z ${homepage} ]] && return 1

	if ! _which xdg-open; then
		echo "xdg-open missing, install xdg-utils"
		return 1
	fi

	xdg-open "${homepage}"
}
