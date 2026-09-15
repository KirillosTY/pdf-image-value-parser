-- Proposed layout only; not executed. Shared images table must already exist.

CREATE TABLE extracted_50542627ef06c434_measurements (
	image_key TEXT NOT NULL, 
	ordinal INTEGER NOT NULL, 
	panel TEXT NOT NULL, 
	chart_type TEXT NOT NULL, 
	title TEXT, 
	series TEXT, 
	category TEXT, 
	row_label TEXT, 
	x NUMERIC, 
	y NUMERIC, 
	value NUMERIC, 
	statistic TEXT, 
	unit TEXT, 
	reading TEXT NOT NULL, 
	notes TEXT, 
	PRIMARY KEY (image_key, ordinal), 
	FOREIGN KEY(image_key) REFERENCES images (image_key)
)

;
