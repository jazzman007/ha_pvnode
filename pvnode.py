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

    wh_hours = {}
    watts = {}

    def __init__(self, kWp: float,  data: dict):
        self.kWp = kWp
        self.api_timezone = ZoneInfo(data['data_timezone'])
        self.last_update = self.now()

        tmp = {}
        for v in data['values']:
            date = datetime.fromisoformat(v['dtm'])
            # move everything one minute back to make sure h:00 time gets
            # accounted in (h-1):00 - (h-1):59 slot
            # TODO: make it better
            date = date.replace(tzinfo=self.api_timezone) - timedelta(minutes=1)
            date = date.replace(minute=0, second=0, microsecond=0)
            self.watts[date] = v['spec_watts'] * self.kWp
            if date in tmp:
                tmp[date].append(v['spec_watts'])
            else:
                tmp[date] = [v['spec_watts']]

        for t, v in tmp.items():
            self.wh_hours[t] = sum(v) / len(v) * self.kWp


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
        return _timed_value(time, self.watts) or 0


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
            (watt for date, watt in self.watts.items() if date.date() == specific_date),
            default=None,
        )
        for timestamp, watt in self.watts.items():
            if watt == value:
                return timestamp
        raise RuntimeError("No peak production time found")

    def get_last_update(self) -> datetime:
        return self.last_update


class PVNode:

    estimate_cached = None

    def __init__(self, api_key, latitude, longitude, slope, orientation, kWp, instheight, instdate, time_zone):
        self.api_key = api_key
        self.latitude = latitude
        self.longitude = longitude
        self.slope = slope
        self.orientation = orientation
        self.kWp = kWp
        self.instheight = instheight
        self.instdate = instdate
        self.time_zone = time_zone
    
    async def estimate(self):
        if self.estimate_cached and self.estimate_cached.now() < (self.estimate_cached.last_update + timedelta(hours=8)):
            return self.estimate_cached
        self.estimate_cached = await asyncio.get_running_loop().run_in_executor(None, self._estimate) 
        return self.estimate_cached

    def _estimate(self):
        panel_age_years = (date.today() - date.fromisoformat(self.instdate)).days / 365.25
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
            "panel_age_years": panel_age_years 
#            "pv_technology_type": "monosi",
        }
        headers = {
            'Authorization': 'Bearer ' + self.api_key
        }
        response = requests.get(url, headers=headers, params=body)
        return Estimate(self.kWp, response.json())
