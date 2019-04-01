#!/usr/bin/env python3

"""Contains code for the annotation process"""
import pysam
from functools import reduce
import re
import numpy as np
import logging
import datetime

def get_frozenset(adict):
    return frozenset((key, set_from_dict(val) if isinstance(val, dict) else val) for key, val in adict.items())

def create_anno_dict(peak, hit):
	""" Returns a dictionary containing information on the hit from gtf """

	#Add peak information
	anno_dict = {}
	anno_dict.update(peak) #fills out peak chr/start/end/id/score/strand
	anno_dict["peak_center"] = int((anno_dict["peak_end"] + anno_dict["peak_start"])/2)
	anno_dict["peak_length"] = anno_dict["peak_end"] - anno_dict["peak_start"]

	#Parse info from gtf string
	try:
		pairs = re.split(";\s*", hit.attributes) #regex remove 0 to n spaces
		pairs = [pair.replace("\"", "") for pair in pairs]
		attribute_dict = {pair.split()[0]:pair.split()[1] for pair in pairs if pair != ""} # parse " of attribute values
	except:
		print("Error reading attributes: {0}".format(hit.attributes))
		attribute_dict = {}

	# Fill in with feature info
	anno_dict["feature"] = hit.feature
	anno_dict["feat_strand"] = hit.strand
	anno_dict["feat_start"] = int(hit.start)
	anno_dict["feat_end"] = int(hit.end)
	anno_dict["feat_center"] = int((anno_dict["feat_end"] + anno_dict["feat_start"])/2)
	anno_dict["feat_length"] =  int(anno_dict["feat_end"] - anno_dict["feat_start"])
	anno_dict["feat_attributes"] = attribute_dict

	#Look-up keys for annotation
	anno_dict["anchor_pos"] = {"start": anno_dict["feat_start"] if anno_dict["feat_strand"] != "-" else anno_dict["feat_end"],
								"center": anno_dict["feat_center"], 
								"end": anno_dict["feat_end"] if anno_dict["feat_strand"] != "-" else anno_dict["feat_start"]}

	#Change with each query
	anno_dict["query"] = 0	 #query for which the hit was valid
	anno_dict["best_hit"] = 0

	return(anno_dict)


def distance_to_peak_center(anno_dict, query_anchor):
    """ Assigns the distance of peak center to best query anchor """

    anchor_list = list(query_anchor)

    #Set default if anchor list is empty
    if len(query_anchor) == 0:
    	anchor_list = ["start", "center", "end"]

    #Calculate distances to each possible anchor
    raw_distances = [anno_dict["peak_center"] - anno_dict["anchor_pos"][anchor] for anchor in anchor_list]
    abs_distances = [abs(dist) for dist in raw_distances]
    min_dist_i = abs_distances.index(min(abs_distances))

    #Set minimum distance as best anchor
    anno_dict["raw_distance"] = raw_distances[min_dist_i]
    anno_dict["distance"] = int(abs(raw_distances[min_dist_i]))
    anno_dict["feat_anchor"] = anchor_list[min_dist_i]

    return(anno_dict)


# import "division" allows decimals
def calculate_overlap(anno_dict):
    """ Calculates percentage of length covered by the peak/feature """
    
    #beds exclude first position, therefore +1 for starts. Range excludes last position in range - therefore +1 for end
    ovl_range = range(max(anno_dict["peak_start"]+1, anno_dict["feat_start"]+1), min(anno_dict["peak_end"], anno_dict["feat_end"])+1)	
    ovl_bp = len(ovl_range)

   	#peak_range = list(range(anno_dict["peak_start"], anno_dict["peak_end"]))
    #feature_range = list(range(anno_dict["feat_start"], anno_dict["feat_end"]))
    #ovl_range = set(peak_range).intersection(feature_range)
    
    ovl_pk = round(ovl_bp /  anno_dict["peak_length"], 3) 
    ovl_feat = round(ovl_bp / anno_dict["feat_length"], 3) 

    anno_dict["feat_ovl_peak"] = ovl_feat
    anno_dict["peak_ovl_feat"] = ovl_pk

    return(anno_dict)


def get_relative_location(anno_dict):
	""" Sets the relative location of peak to feature """

	if anno_dict["peak_start"] <= anno_dict["feat_start"] and anno_dict["peak_end"] >= anno_dict["feat_end"]:
		location = "FeatureInsidePeak"

	elif anno_dict["peak_start"] > anno_dict["feat_start"] and anno_dict["peak_end"] < anno_dict["feat_end"]:
		location = "PeakInsideFeature"

	elif anno_dict["feat_anchor"] == "start":
		if anno_dict["feat_ovl_peak"] > 0:
			location = "OverlapStart"
		else:
			if anno_dict["feat_strand"] == "+":
				location = "Upstream"
			else:
				location = "Downstream"

	elif anno_dict["feat_anchor"] == "end":
		if anno_dict["feat_ovl_peak"] > 0:
			location = "OverlapEnd"
		else:
			if anno_dict["feat_strand"] == "+":
				location = "Downstream"
			else:
				location = "Upstream"
	else:
		location = "NA"

	anno_dict["relative_location"] = location

	return(anno_dict)


def annotate_peaks(peaks, gtf_gz, gtf_index, cfg_dict, logger=None):
	""" Peaks is a list of tuple-elements (chrom, start, end, name, ...) 
		gtf_gz and gtf_index relate to the tabix gtf file
		cfg-dict is the loaded config containing queries
	"""

	if logger is None:
		logger = logging.getLogger('')	#local logger leading to nowhere

	#Open tabix file
	tabix_obj = pysam.TabixFile(gtf_gz, index=gtf_index)

	#Information on queries
	queries = cfg_dict["queries"]
	n_queries = len(queries)
	distances = sum([query["distance"] for query in queries], [])
	max_distance = int(max(distances))

	#For each peak in input peaks, collect all_valid_annotations
	all_valid_annotations = []
	for peak in peaks:
		logger.debug("\n\n")
		logger.debug("Annotating peak: {0}".format(peak))

		valid_annotations = []	#for this peak
		stop_searching = False
		query_i = -1
		while query_i+1 < n_queries and stop_searching == False:

			query_i += 1				#First query is 0
			query = queries[query_i]	#current query to check

			logger.debug("Finding hits for query: {0}".format(query))

			#Extend and fetch possible hits from tabix
			max_distance = max(query["distance"])
			extend_start = int(max(1, peak["peak_start"] - max_distance))
			extend_end = peak["peak_end"] + max_distance
			tabix_query = "{0}:{1}-{2}".format(peak["gtf_chr"], extend_start, extend_end)
			logger.debug("Tabix query for query {0} ({1}): {2}".format(query_i, query["name"], tabix_query))

			try:
				begin = datetime.datetime.now()
				hits = list(tabix_obj.fetch(peak["peak_chr"], extend_start, extend_end, parser=pysam.asGTF()))	#hits for this query
				end = datetime.datetime.now()
				logger.debug("Fetched {0} hits in {1}".format(len(hits), end - begin))
			except: 
				#exception if no hits could be fetched from tabix, for example if the contig does not exist in the gtf index.
				#print("ERROR: Could not create iterator for peak: {0} \n with tabix query {1}".format(peak, tabix_query))		
				stop_searching = True 
				hits = []

			begin = datetime.datetime.now()
			for hit in hits: 
				
				#If feature is not the right one, we do not have to go further - saves computation of distances
				if "feature" in query:
					if hit.feature not in query["feature"]:
						#logger.debug("{0} not in {1} - continuing to next hit".format(hit.feature, query["feature"]))
						continue

				anno_dict = create_anno_dict(peak, hit)
				anno_dict["query"] = query_i
				anno_dict["query_name"] = query["name"]

				#Calculate distances/relative location
				anno_dict = distance_to_peak_center(anno_dict, query.get("feature_anchor", []))
				anno_dict = calculate_overlap(anno_dict)
				anno_dict = get_relative_location(anno_dict)

				##### Test validity of the hit to query_i ####
				checks = {}
				#if "feature" in query:
				#	checks["feature"] = anno_dict["feature"] in query["feature"]	 #feature

				#Check feature anchor
				if "feature_anchor" in query:
					checks["feature_anchor"] = anno_dict["feat_anchor"] in query["feature_anchor"]

				#Peak strand relative to feature strand
				if "strand" in query:
					if query["strand"] != "ignore" and anno_dict["peak_strand"] != ".":
						checks["strand"] = ((anno_dict["peak_strand"] == anno_dict["feat_strand"] and query["strand"] == "same") or 
											(anno_dict["peak_strand"] != anno_dict["feat_strand"] and query["strand"] == "opposite"))	

				#Check whether distance was valid
				if anno_dict["feat_strand"] == "+":
					checks["distance"] = anno_dict["raw_distance"] > -query["distance"][0] and anno_dict["raw_distance"] < query["distance"][1]
				else:
					checks["distance"] = anno_dict["raw_distance"] > -query["distance"][1] and anno_dict["raw_distance"] < query["distance"][0]

				#Check distance (Distance can still be valid if PeakInsideFeature/FeatureInsidePeak and internals flag is set)
				if "internals" in query:
					max_overlap = max(anno_dict["feat_ovl_peak"], anno_dict["peak_ovl_feat"])
					checks["distance"] = checks["distance"] or (query["internals"]*1.0 > 0 and max_overlap >= query["internals"]*1.0)	#if internals is set to more than 0 overlap

				#Filter on relative location
				if "relative_location" in query:
					checks["relative_location"] = anno_dict["relative_location"] in query["relative_location"]
				
				#Filter on attribute if any was set
				if "filter_attribute" in query and "attribute_values" in query: #query["filter_attribute"] != None:
					checks["attribute"] = anno_dict["feat_attributes"].get(query["filter_attribute"], None) in query["attribute_values"]

				##### All checks are done -> establish if hit is a valid annotation #####
				valid = sum(checks.values()) == len(checks.values()) #all checks must be valid
				if valid:
					valid_annotations.append(anno_dict.copy())
				logger.debug("Validity: {0} | Checks: {1} | Annotation dict: {2}".format(valid, checks, {key:anno_dict[key] for key in anno_dict}))
			
			end = datetime.datetime.now()
			logger.debug("Validated hits in {0}".format(end-begin))

			#All tabix hits for this query were checked - if priority, stop searching if any valid hit was found -> else, check next query
			stop_searching = (len(valid_annotations) > 0 and cfg_dict["priority"]) or stop_searching #or if stop_searching was already set previously

			if stop_searching == True:
				logger.debug("{0} valid hit(s) were found for query_i = {1} and priority is true - stopping search.".format(len(valid_annotations), query_i))
			else:
				logger.debug("A total of {0} valid hits were found. Incrementing query_i.".format(len(valid_annotations), query_i))
		
		logger.debug("")	#create empty line in debugger output for easy overview

		#After all hits have been checked for peak; make final checks and set best flag
		if len(valid_annotations) > 0:	

			#If priority == True, find the highest ranked annotations for this peak
			#if cfg_dict["priority"] == True:
			#	highest_priority_query = min([anno_dict["query"] for anno_dict in valid_annotations])
			#	#logger.debug("Highest priority hit for {0} was {1}".format(peak, highest_priority_query))
			#	valid_annotations = [anno_dict for anno_dict in valid_annotations if anno_dict["query"] == highest_priority_query]

			distances = [anno_dict["distance"] for anno_dict in valid_annotations]
			valid_annotations[distances.index(min(distances))]["best_hit"] = 1

		else:
			valid_annotations = [peak]
			valid_annotations[0]["best_hit"] = 1			#the empty hit for the peak is the best hit

		#Add result to all the valid annotations 
		all_valid_annotations.extend(valid_annotations)

	tabix_obj.close()

	return(all_valid_annotations)
