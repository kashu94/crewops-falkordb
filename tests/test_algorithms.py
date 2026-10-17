from crewops.algorithms import knock_on, network_components, positioning_routes, station_criticality, who_knows


def test_the_hub_is_the_most_critical_station(loaded):
    ranked = station_criticality(loaded.ops)
    assert ranked[0]["station"] == "BLR"
    assert ranked[0]["betweenness"] > 0
    assert ranked[0]["movements"] > ranked[1]["movements"]


def test_positioning_between_spokes_goes_through_the_hub(loaded):
    routes = positioning_routes(loaded.ops, "goi", "ccu")
    assert routes[0]["stations"] == ["GOI", "BLR", "CCU"]


def test_route_network_is_one_component(loaded):
    assert len(network_components(loaded.ops)) == 1


def test_first_leg_of_the_day_has_the_most_knock_on(loaded):
    top = knock_on(loaded.ops, "2026-10-14", limit=1)[0]
    assert top["downstream"] >= 3


def test_who_knows_a_station(loaded):
    rows = who_knows(loaded.ops, station="DEL")
    assert rows and rows[0]["legs"] >= rows[-1]["legs"]
    assert who_knows(loaded.ops, aircraft_type="atr72")[0]["crew_id"].startswith("C-")
