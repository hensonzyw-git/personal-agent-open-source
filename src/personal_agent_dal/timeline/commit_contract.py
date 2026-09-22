"""Exact Git message bytes shared by execution and lost-response recovery."""

def commit_message(stage):
    subject=stage.get('goal',{}).get('commit',{}).get('subject')
    if subject is None:subject='DAL stage '+stage['stage_id']+'/'+str(stage['revision'])
    if not isinstance(subject,str) or not subject.strip() or len(subject.encode())>256 or any(ord(c)<32 for c in subject):
        raise ValueError('COMMIT_PLAN_INVALID')
    return (subject+'\n').encode()
