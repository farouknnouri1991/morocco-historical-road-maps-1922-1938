rm(list = ls())

full_args <- commandArgs(trailingOnly = FALSE)
script_arg <- sub("^--file=", "", full_args[grep("^--file=", full_args)])
if (!length(script_arg)) stop("Unable to locate this R script")
root <- normalizePath(file.path(dirname(script_arg[[1]]), ".."), winslash = "/", mustWork = TRUE)
python <- normalizePath(file.path(root, "..", "REPRODUCTION_1938", "environnement", "python_historique", "venv", "Scripts", "python.exe"), winslash = "/", mustWork = TRUE)

config_path <- file.path(root, "config", "core28_measurement_from_routable_maps.json")
producer <- file.path(root, "code", "50_build_core28_routable_network.py")
if (!file.exists(config_path)) stop("COLONNE MANQUANTE : config/core28_measurement_from_routable_maps.json")
if (!file.exists(producer)) stop("COLONNE MANQUANTE : code/50_build_core28_routable_network.py")

status <- system2(python, c(producer, "--config", config_path))
if (!identical(status, 0L)) stop("Core-28 map-derived routable-network construction failed with status ", status)
message("Wrote the Core-28 measurement package inside historical maps.")
