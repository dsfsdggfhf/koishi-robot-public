"""Calendar selection, with no guessed weather or regional identity."""
from lunar_python import Solar


def matches(rule,day,flags):
    kind=rule['calendar']
    lunar=Solar.fromYmd(day.year,day.month,day.day).getLunar()
    if kind=='solar':return (day.month,day.day)==(rule['month'],rule['day'])
    if kind=='lunar':return (lunar.getMonth(),lunar.getDay())==(rule['month'],rule['day'])
    if kind=='lunar_year_last_day':
        tomorrow=Solar.fromYmd(day.year,day.month,day.day).next(1).getLunar()
        return tomorrow.getMonth()==1 and tomorrow.getDay()==1
    if kind=='solar_term':return lunar.getJieQi()==rule['name']
    if kind=='lunar_regional':
        chosen=flags.get('little_new_year_day')
        return chosen in (23,24) and lunar.getMonth()==12 and lunar.getDay()==chosen
    if kind=='nth_weekday':return day.month==rule['month'] and day.weekday()==6 and (day.day-1)//7+1==rule['occurrence']
    return False


class Calendar:
    def __init__(self,manifest,holidays,terms):
        self.events=manifest['events']
        self.holidays={i['name']:i['msg'] for i in holidays['items']}
        self.terms={i['solar_term']:i for i in terms['items']}
        assert '清明' not in self.terms

    def choose(self,day,flags):
        for event in self.events:
            if event['label'] in ('用户生日','清明'):continue
            mode=event['mode']
            if mode=='preference_or_mention' and event['id'] not in flags.get('holiday_optins',[]):continue
            if mode=='confirmed_identity_or_mention' and event['id'] not in flags.get('holiday_optins',[]):continue
            if matches(event['date_rule'],day,flags):
                return event['id'],self.holidays[event['template_name']]
        term=Solar.fromYmd(day.year,day.month,day.day).getLunar().getJieQi()
        if term in self.terms:
            entry=self.terms[term]
            return entry.get('shared_event_id') or 'term:'+term,entry['msg']
        return None
