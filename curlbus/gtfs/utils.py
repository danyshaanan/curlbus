"gtfs/utils.py: GTFS-related utilitiesd"
# Copyright (C) 2018 Elad Alfassa <elad@fedoraproject.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: GPL-3.0-or-later
from typing import List
import geopy.distance
from aiocache import cached
from sqlalchemy import func, select
from .model import Trip, Route, Stop, Agency, Translation, City
from .town_synonyms import towns
from ..operators import operator_names, operators

MINUTES = 60*60


class cached_no_db(cached):
    def get_cache_key(self, f, args, kwargs):
        # remove "db" from args for the cache key
        return self._key_from_args(f, args[1:], kwargs)


@cached_no_db(ttl=30*MINUTES)
async def translate_city_name(db, city):
    ret = None
    # Prefer the official GTFS translation database
    city_translation = await Translation.get(db, city)
    if 'EN' in city_translation:
        ret = city_translation['EN']
    else:
        # Fall back to the government city database
        city = await db.first(City.query.where(City.name == city))
        if city is not None and city != "":
            ret = city.english_name
    return ret


@cached_no_db(ttl=30*MINUTES)
async def get_translated_address(db, stop):
    """ Translates the 'city' value in a stop's address, if possible """
    address = stop.address
    translation = await translate_city_name(db, stop.address['city'])
    if translation is not None:
        address['city'] = translation
    return address


async def get_arrival_gtfs_info(arrival, db):
        trip_query = db.first(Trip.query.where(Trip.trip_id == str(arrival.trip_id)))
        agency_query = db.first(Agency.query.where(Agency.agency_id == arrival.operator_id))

        trip = await trip_query
        agency = await agency_query

        destination = None
        destination_name = None
        destination_address = None

        headsign = None
        agency = {"name": {"HE": agency.agency_name,
                           "EN": operator_names[int(arrival.operator_id)]},
                  "url": agency.agency_url}

        if trip:
            # Prefer destination from trip
            last_stop = await trip.get_last_stop_code(db)
            destination = await db.first(Stop.query.where(Stop.stop_code == last_stop))
            destination_name = await Translation.get(db, destination.stop_name)
            if trip.trip_headsign is not None:
                headsign = await Translation.get(db, trip.trip_headsign)
        else:
            # if we don't have a trip (for example if the GTFS feed is broken)
            # try using destination_id from the realtime data
            destination = await db.first(Stop.query.where(Stop.stop_code == str(arrival.destination_id)))
            if destination is not None:
                destination_name = await Translation.get(db, destination.stop_name)
            else:
                # Sometimes the GTFS feed is so broken that it doesn't have
                # some stops that the realtime data does have.
                destination_name = None
            headsign = None

        if destination:
            destination_address = await get_translated_address(db, destination)
        else:
            destination_address = None

        if destination:
            destination = {"code": destination.stop_code,
                           "name": destination_name,
                           "address": destination_address,
                           "location": {"lat": destination.stop_lat,
                                        "lon": destination.stop_lon}}
        else:
            destination = None
        return {"destination": destination,
                "agency": agency,
                "headsign": headsign}


@cached_no_db(ttl=15*MINUTES)
async def get_stop_info(db_session, stop_code):
    query = Stop.query.where(Stop.stop_code == stop_code).limit(1)
    query.bind = db_session
    stop = await query.gino.first()
    if stop is None:
        return None

    return {"name": await Translation.get(db_session, stop.stop_name),
            "address": await get_translated_address(db_session, stop),
            "location": {"lat": stop.stop_lat,
                         "lon": stop.stop_lon}}


@cached_no_db(ttl=30*MINUTES)
async def get_routes(db, operator_id, route_name):
    """ Get info for route by operator and name """
    routes = await db.all(Route.query.where(Route.agency_id == str(operator_id)).where(
                                            Route.route_short_name == route_name))
    # sorted by id to make sure the order is consistent when there are
    # multiple routes with the same name and operator
    return sorted(routes, key=lambda r: r.route_id)


@cached_no_db(ttl=15*MINUTES)
async def get_route_route(db, route_id, direction_id):
    """ Get an ordered list of stop objects that represent the route of a specific bus line """
    # 1. Find a trip for this route
    # 2. Find StopTimes for this trip, build a dict mapping code -> sequence location
    # 3. Query stops, return sorted by lambda stop: sequence[stop.stop_code]
    # TODO direction_id!!!
    trip = await db.first(Trip.query.where(Trip.route_id == route_id))
    if trip is None:
        return None
    stoptimes = await trip.get_stop_times(db)

    sequence = {stoptime.stop_id: stoptime.stop_sequence for stoptime in stoptimes}

    stops = await db.all(Stop.query.where(Stop.stop_id.in_(sequence.keys())))
    for stop in stops:
        # Translate addresses
        stop.address = await get_translated_address(db, stop)
        stop.translated_name = await Translation.get(db, stop.stop_name)
    return sorted(stops, key=lambda stop: sequence[stop.stop_id])


@cached_no_db(ttl=30*MINUTES)
async def translate_route_name(db, route):
    """ Attempt to find a translation for a route's long name """
    # long route names are in the following format:
    # route_long_name = רדינג-תל אביב יפו<->ת. מרכזית ת''א ק. 4/הורדה-תל אביב יפו-10
    # so a bunch of splits should get us proper translation
    origin, destination = route.route_long_name.split('<->')

    async def translate_part(part):
        # in some cases we don't want to split at all
        full_part_translation = await Translation.get(db, part, lang='EN')
        if full_part_translation is not None:
            return full_part_translation

        # But in other cases, we need to do some heuristics
        stop_name = part.split('-')[0].strip()
        stop_name_translation = await Translation.get(db, stop_name, lang='EN')
        if stop_name_translation is not None:
            stop_name = stop_name_translation.translation
        town = part.split('-')[1].strip()
        town_translation = await translate_city_name(db, town)
        if town_translation is not None:
            town = town_translation

        if town.replace("-", "") in stop_name or town in stop_name:
            # To avoid extra useless data such as "Kiryat Ono Terminal-Kiryat Ono"
            # or the horrifying "Modi'in Macabim Re'ut Central Station/Alighting-Modi'in-Makabim-Re'ut"
            return stop_name
        if town in towns and towns[town].match(stop_name):
            return stop_name
        else:
            return f"{stop_name}-{town}"

    return await translate_part(origin) + "<->" + await translate_part(destination)


@cached_no_db(ttl=30*MINUTES)
async def count_routes(db, operator_id: int) -> int:
    """ Count how many routes an operator has """
    # This query uses route_desc, which contains the official route license number
    # this field is separated by minus, and the first item is the route's
    # unique license number (after that it's "direction code" and "alternative".
    # Using PostgreSQL's split_part function, count, and distinct we can use
    # this to quickly count the routes per operator.
    query = select([func.count(func.distinct(func.split_part(Route.route_desc, '-', 1)))])
    return await db.scalar(query.where(Route.agency_id == str(operator_id)))


@cached_no_db(ttl=30*MINUTES)
async def get_rail_stations(db):
    """ Get all Israel Railways stations in the system """
    rail_agency_id = operators['rail']
    stops = await db.all(f"""SELECT distinct s.stop_code, s.stop_name
                             FROM stops AS s
                             JOIN stoptimes AS st ON st.stop_id=s.stop_id
                             JOIN trips as t ON t.trip_id=st.trip_id
                             JOIN routes as r ON r.route_id=t.route_id
                             WHERE r.agency_id='{rail_agency_id}'""")
    ret = []
    for stop_code, stop_name in stops:
        name = await Translation.get(db, stop_name)
        if 'EN' not in name:
            city_name = await db.first(City.query.where(City.name == stop_name))
            if city_name is not None:
                name['EN'] = city_name.english_name
        ret.append({"code": stop_code,
                    "name": name})
    return ret


@cached_no_db(ttl=60*MINUTES)
async def get_nearby_stops(db, lat: float, lon: float, radius: int):
    """ Get stops in the specified radius (in meters) """

    radius_in_km = radius/1000
    latlon = (lat, lon)

    # Build the (square) bounding box
    distance = geopy.distance.distance()
    north = distance.destination(latlon, 0, radius_in_km).latitude
    east = distance.destination(latlon, 90, radius_in_km).longitude
    south = distance.destination(latlon, 180, radius_in_km).latitude
    west = distance.destination(latlon, -90, radius_in_km).longitude

    # Get a list of stops

    stops = await db.all(Stop.query.
                         where(Stop.stop_lon < east).
                         where(Stop.stop_lon > west).
                         where(Stop.stop_lat < north).
                         where(Stop.stop_lat > south))
    # the gino way to do sql AND is ugly :(

    ret = []
    for stop in stops:
        dist = geopy.distance.distance(latlon, (stop.stop_lat, stop.stop_lon)).meters
        # Narrow down the returned list into a circular radius
        if dist <= radius:
            name = await Translation.get(db, stop.stop_name)
            ret.append({"code": stop.stop_code,
                        "name": name,
                        "distance": round(dist, 2),
                        "location": {"lat": stop.stop_lat,
                                     "lon": stop.stop_lon}})

    return ret
