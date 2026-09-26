package main

// average returns the integer mean of values.
//
// SEEDED BUG: an empty slice makes len(values) zero, so this panics with an
// integer divide-by-zero instead of returning 0 (or an error).
func average(values []int) int {
	sum := 0
	for _, v := range values {
		sum += v
	}
	return sum / len(values)
}
