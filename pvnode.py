from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from dataclasses import dataclass
import requests, asyncio


def _interval_value_sum(interval_begin: datetime, interval_end: datetime, data: dict[datetime, int]) -> int:
    """Return the sum of values in interval."""
    total = 0

    for timestamp, wh in data.items():
        # Skip all until this hour
        if timestamp <= interval_begin:
            continue

        if timestamp > interval_end:
            break

        total += wh

    return total


def _timed_value(at: datetime, data: dict[datetime, int]) -> int | None:
    """Return the value for a specific time."""
    value = None
    for timestamp, cur_value in data.items():
        if timestamp > at:
            return value
        value = cur_value

    return None


class PVNodeConnectionError(Exception):
    '''PVNode connection error'''


@dataclass
class Estimate:

    def __init__(self, kWp: float, data: dict):
        self.kWp = kWp
        self.api_timezone = ZoneInfo(data['data_timezone'])
        self.last_update = self.now()

        
        self.wh_hours = {}
        self.weather_hours = {}
        self.data = {}

        def _add_measurement(dt, date, key, value):
            if key == 'spec_watts':
                value *= self.kWp

            if key not in self.data:
                self.data[key] = {}
                
            self.data[key][dt] = value

            if date not in self.weather_hours:
                self.weather_hours[date] = {}

            if key not in self.weather_hours[date]:
                self.weather_hours[date][key] = []

            self.weather_hours[date][key].append(value)
            
        for value in data['values']:
            dt = datetime.fromisoformat(value['dtm'])
            # move everything one minute back to make sure h:00 time gets
            # accounted in (h-1):00 - (h-1):59 slot
            # TODO: make it better
            dt = dt.replace(tzinfo=self.api_timezone) - timedelta(minutes=1)
            date = dt.replace(minute=0, second=0, microsecond=0)

            for k, v in value.items():
                if k == 'dtm':
                    continue
                _add_measurement(dt, date, k, v)

        for t, v in self.weather_hours.items():
            for k, m in v.items():
                if k == 'weather_code':
                    self.weather_hours[t][k] = max(m)
                else:
                    self.weather_hours[t][k] = sum(m) / len(m)

            self.wh_hours[t] = v['spec_watts']


    @property
    def energy_production_today(self) -> int:
        return self.day_production(self.now().date())


    @property
    def energy_production_today_remaining(self) -> int:
        return _interval_value_sum(
            self.now(),
            self.now().replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1),
            self.wh_hours,
        )


    @property
    def energy_production_tomorrow(self) -> int:
         return self.day_production(self.now().date() + timedelta(days=1))


    @property
    def power_highest_peak_time_today(self) -> datetime:
        return self.peak_production_time(self.now().date())


    @property
    def power_highest_peak_time_tomorrow(self) -> datetime:
        return self.peak_production_time(self.now().date() + timedelta(days=1))


    @property
    def power_production_now(self) -> int:
         return self.power_production_at_time(self.now())

    def now(self) -> datetime:
        return datetime.now(tz=self.api_timezone)

    @property
    def energy_current_hour(self) -> int:
        return _timed_value(self.now().replace(minute=0, second=0, microsecond=0), self.wh_hours) or 0
    

    @property
    def last_update(self) -> datetime:
        return self._last_update


    @last_update.setter
    def last_update(self, value) -> None:
        self._last_update = value


    def power_production_at_time(self, time: datetime) -> int:
        return _timed_value(time, self.data['spec_watts']) or 0


    def sum_energy_production(self, period_hours: int) -> int:
        now = self.now().replace(minute=59, second=59, microsecond=999)
        until = now + timedelta(hours=period_hours)

        return _interval_value_sum(now, until, self.wh_hours)


    def day_production(self, specific_date: date) -> int:
        fr = datetime.combine(specific_date, datetime.min.time(), self.api_timezone)
        until = datetime.combine(specific_date, datetime.max.time(), self.api_timezone)

        return _interval_value_sum(fr, until, self.wh_hours)


    def peak_production_time(self, specific_date: date) -> datetime:
        value = max(
            (watt for date, watt in self.data['spec_watts'].items() if date.date() == specific_date),
            default=None,
        )
        for timestamp, watt in self.data['spec_watts'].items():
            if watt == value:
                return timestamp
        raise RuntimeError("No peak production time found")


    def get_last_update(self) -> datetime:
        return self.last_update

    
    @property
    def weather_temperature_now(self) -> int:
        return _timed_value(self.now(), self.data["temp"]) or 0


    @property
    def weather_precipitation_now(self) -> int:
        return _timed_value(self.now(), self.data["precip"]) or 0


    @property
    def weather_humidity_now(self) -> int:
        return _timed_value(self.now(), self.data["RH"]) or 0


    @property
    def weather_code_now(self) -> int:
        return _timed_value(self.now(), self.data["weather_code"]) or 0


    @property
    def weather_wind_speed_now(self) -> int:
        return _timed_value(self.now(), self.data["vwind"]) or 0


class PVNode:

    estimate_cached = None

    def __init__(self, api_key, latitude, longitude, slope, orientation, kWp, instheight, instdate, time_zone, technology, obstruction, weather_enabled=False):
        self.api_key = api_key
        self.latitude = latitude
        self.longitude = longitude
        self.slope = slope
        self.orientation = orientation
        self.kWp = kWp
        self.instheight = instheight
        self.instdate = instdate
        self.time_zone = time_zone
        self.technology = technology
        self.obstruction = obstruction
        self.weather_enabled = weather_enabled
    
    async def estimate(self):
        if self.estimate_cached and self.estimate_cached.now() < (self.estimate_cached.last_update + timedelta(hours=3)):
            return self.estimate_cached

        self.estimate_cached = await asyncio.get_running_loop().run_in_executor(None, self._estimate) 
        return self.estimate_cached

    def _estimate(self):
        url = 'https://api.pvnode.com/v1/forecast/'
        body = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "slope": self.slope,
            "orientation": self.orientation,
            "past_days": 0,
            "forecast_days": 1,
            "required_data": "spec_watts",
            "installation_height": self.instheight,
            "timezone": self.time_zone,
        }
        if self.instdate and len(self.instdate) > 0:
            body["panel_age_years"] = (date.today() - date.fromisoformat(self.instdate)).days / 365.25
        if self.technology and len(self.technology) > 0:
            body["pv_technology_type"] =  self.technology
        if self.obstruction and len(self.obstruction) > 0:
            body["sky_obstruction_config"] = self.obstruction
        if self.weather_enabled:
            body["required_data"] = "spec_watts,temp,RH,precip,vwind,weather_code"
        
        headers = {
            'Authorization': 'Bearer ' + self.api_key
        }

        response = requests.get(url, headers=headers, params=body)

        if response.status_code == 400:
            raise PVNodeConnectionError('API Key wrong?')
        elif response.status_code == 404:
            raise PVNodeConnectionError(f"Parameters wrong? {response.json()['detail']}")
        elif response.status_code > 400:
            raise PVNodeConnectionError('Something went wrong ...')

        return Estimate(self.kWp, response.json())
