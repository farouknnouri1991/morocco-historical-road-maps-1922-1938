rm(list = ls())

full_args <- commandArgs(trailingOnly = FALSE)
script_arg <- sub("^--file=", "", full_args[grep("^--file=", full_args)])
if (!length(script_arg)) stop("Unable to locate this R script")
root <- normalizePath(file.path(dirname(script_arg[[1]]), ".."), winslash = "/", mustWork = TRUE)
python <- normalizePath(file.path(root, "..", "REPRODUCTION_1938", "environnement", "python_historique", "venv", "Scripts", "python.exe"), winslash = "/", mustWork = TRUE)

args <- commandArgs(trailingOnly = TRUE)
config_path <- if (length(args)) {
  candidate <- args[[1]]
  if (!grepl("^([A-Za-z]:|/)", candidate)) candidate <- file.path(root, candidate)
  normalizePath(candidate, winslash = "/", mustWork = TRUE)
} else file.path(root, "config", "routable_topology_1922_1925.json")
producer <- file.path(root, "code", "52_build_routable_topology_from_maps.py")
if (!file.exists(config_path)) stop("COLONNE MANQUANTE : configuration de topologie parcourable")
if (!file.exists(producer)) stop("COLONNE MANQUANTE : code/52_build_routable_topology_from_maps.py")

status <- system2(python, c(producer, "--config", config_path))
if (!identical(status, 0L)) stop("Map-derived routable topology repair failed with status ", status)
message("Wrote the complete map-production package inside historical maps.")
