"""
Views for trials.

TODO: separate this out into a logically separated package at some point.
"""
import datetime
import random

from django.conf import settings
from django.core.urlresolvers import reverse
from django.contrib.auth import authenticate, login
from django.contrib.auth.decorators import login_required
from django.forms.formsets import all_valid
from django.forms.models import inlineformset_factory
from django.http import HttpResponse, HttpResponseRedirect, HttpResponseForbidden
from django.utils.decorators import method_decorator
from django.views.generic import DetailView, TemplateView, View, ListView
from django.views.generic.edit import CreateView, BaseCreateView, UpdateView, FormView
from django.utils import simplejson
from extra_views import CreateWithInlinesView, InlineFormSet
from extra_views import NamedFormsetsMixin, ModelFormSetView
from extra_views.advanced import BaseCreateWithInlinesView
import ffs
from letter.contrib.contact import ContactView

from rm import exceptions
from rm.trials.forms import (TrialForm, VariableForm, N1TrialForm, TutorialForm)
from rm.trials.models import Trial, Report, Variable, Invitation, TutorialExample
from rm.trials.utils import n1_with_sane_defaults
from rm.userprofiles.models import RMUser
from rm.userprofiles.utils import sign_me_up

def serve_maybe(meth):
    """
    Decorator to figure out if we want to serve files
    ourselves (DEBUG) or hand off to Nginx
    """
    # Originally from Open Prescribing raw.views

    def handoff(self, *args, **kwargs):
        """
        Internal wrapper function to figure out
        the logic
        """
        filename = meth(self, *args, **kwargs)

        # When we're running locally, just take the hit, otherwise
        # offload the serving of the datafile to Nginx
        if settings.DEBUG:
            resp = HttpResponse(
                open(filename, 'rb').read(),
                mimetype='application/force-download'
                )
            return resp

        resp = HttpResponse()
        url = '/protected/{0}'.format(filename)
        # let nginx determine the correct content type
        resp['Content-Type']=""
        resp['X-Accel-Redirect'] = url
        return resp

    return handoff


class JsonResponse(HttpResponse):
    """
        JSON response
    """
    def __init__(self, content, mimetype='application/json', status=None, content_type=None):
        super(JsonResponse, self).__init__(
            content=simplejson.dumps(content),
            mimetype=mimetype,
            status=status,
            content_type=content_type,
        )

class LoginRequiredMixin(object):
    """
    View mixin which verifies that the user has authenticated.

    NOTE:
        This should be the left-most mixin of a view.
    """

    @method_decorator(login_required)
    def dispatch(self, *args, **kwargs):
        return super(LoginRequiredMixin, self).dispatch(*args, **kwargs)


class TrialByPkMixin(object):
    """
    set the trial as an attr.
    """

    def dispatch(self, *args,**kw):
        if not getattr(self, 'trial', None):
            self.trial = Trial.objects.get(pk=kw['pk'])
        return super(TrialByPkMixin, self).dispatch(*args, **kw)

    def get(self, *args,**kw):
        """
        Store the trial isntance
        """
        if not getattr(self, 'trial', None):
            self.trial =Trial.objects.get(pk=kw['pk'])
        return super(TrialByPkMixin, self).get(*args, **kw)

    def post(self, *args,**kw):
        """
        Store the trial isntance
        """
        if not getattr(self, 'trial', None):
            self.trial =Trial.objects.get(pk=kw['pk'])
        return super(TrialByPkMixin, self).post(*args, **kw)

    def get_context_data(self, **kw):
        """
        We want access to the trial data in the template please!
        """
        trial = getattr(self, 'trial', None)
        if not trial:
            raise ValueError()
        context = super(TrialByPkMixin, self).get_context_data(**kw)
        context['trial'] = trial
        return context


class OwnsTrialMixin(object):

    def dispatch(self, *args, **kwargs):
        if self.trial.owner != self.request.user:
            return HttpResponseForbidden('Not Your Trial!')
        return super(OwnsTrialMixin, self).dispatch(*args, **kwargs)


class ReportView(CreateView):
    """
    Generic report data view.

    Ensures that we have access to the right trial at the right time.
    """

    context_object_name = 'report'

    def get(self, *args,**kw):
        """
        Store the trial isntance
        """
        self.trial = self.trial_model.objects.get(pk=kw['pk'])
        return super(ReportView, self).get(*args, **kw)

    def post(self, *args,**kw):
        """
        Add a particular report's data.

        Check to see if this report means we need to stop the trial.
        If we do, stop it.
        """
        self.trial = self.trial_model.objects.get(pk=kw['pk'])
        date = datetime.datetime.strptime(self.request.POST['date'], '%d/%m/%Y').date()
        participant = self.trial.participant_set.get(user=self.request.user)
        group = participant.group

        variable = self.trial.variable_set.all()[0]

        if self.trial.n1trial:

            report = Report.objects.get(trial=self.trial,
                                        date__isnull=True)
            d, m, y = self.request.POST['date'].split('/')
            d, m, y = int(d), int(m), int(y)
            date = datetime.date(y, m, d)
            report.date  = date
            report.save()

        else:
            report = Report.objects.get_or_create(trial=self.trial, date=date,
                                                  participant=participant,
                                                  group=group, variable=variable)[0]
        if variable.style == variable.SCORE:
            report.score = int(self.request.POST['score'])
        elif variable.style == variable.BINARY:
            report.binary = bool(self.request.POST['binary'])
        elif variable.style == variable.COUNT:
            report.count = int(self.request.POST['count'])
        report.save()

        # Checking for closing criteria
        if self.trial.ending_style == self.trial.REPORT_NUM:
            if self.trial.report_set.count() >= self.trial.ending_reports:
                self.trial.stop()


        return HttpResponseRedirect(self.trial.get_absolute_url())

    def get_context_data(self, **kw):
        """
        We want access to the trial data in the template please!
        """
        trial = getattr(self, 'trial', None)
        if not trial:
            raise ValueError()
        context = super(ReportView, self).get_context_data(**kw)
        context['trial'] = trial
        return context


class TrialReport(ReportView):
    """
    Report a data point for this trial
    """
    model       = Report
    trial_model = Trial


class MyTrials(TemplateView):
    """
    Trials associated with this user
    """
    template_name = 'trials/my_trials.html'


class TrialDetail(DetailView):
    """
    A trial detail page - this will be the unique URL for
    a trial.
    """
    context_object_name = "trial"
    model               = Trial

    def get(self, *args, **kw):
        """
        Make sure that we adhere to the right privacy concerns.

        * N=1 private trials == you must be the owner
        * N>1 private trials == you must:
                                * be the owner
                                * be participating
                                * have an email address that matches an
                                  invitation for this trial
        """
        trial = self.get_object()
        if trial.private:
            if self.request.user != trial.owner:
                if trial.n1trial: # n1 private, non owner -> Go home
                    return HttpResponse('Unauthorized', status=401)

                # private trial, no auth -> go home
                elif not self.request.user.is_authenticated():
                    return HttpResponse('Unauthorized', status=401)
                elif trial.participant_set.filter(user=self.request.user).count() > 0:
                    pass # private but we're a participant -> Collect $200
                elif trial.invitation_set.filter(email=self.request.user.email).count() >0:
                    pass # private but we're invited -> Collect $200
                else:            # private, authenticated, not authorized -> Go home
                    return HttpResponse('Unauthorized', status=401)

        return super(TrialDetail, self).get(*args, **kw)

    def get_context_data(self, **kw):
        """
        Determine the detail template appropriate for this user

        Return: dict
        Exceptions: None
        """
        context = super(TrialDetail, self).get_context_data(**kw)
        trial = context['trial']

        detail_template = 'trials/trial_detail_recruiting.html'
        page_title = 'Recruiting Trial'

        if trial.finished:
            detail_template = 'trials/trial_detail_report.html'
            page_title = 'Trial Report'
            if trial.owner == self.request.user:
                context['is_owner'] = True

        elif self.request.user.is_authenticated():
            if trial.owner == self.request.user:
                detail_template = 'trials/trial_detail_owner.html'
                page_title = 'Your Trial'
                context['is_owner'] = True
            elif trial.participant_set.filter(user=self.request.user).count() > 0:
                detail_template = 'trials/trial_detail_participant.html'
                page_title = 'Participating In'
                group = trial.participant_set.get(user=self.request.user).group
                if group is not None:
                    instructions = group.name == 'A' and trial.group_a or trial.group_b
                    context['instructions'] = instructions
                context['participant'] = True

        if trial.recruitment == trial.INVITATION:
            can_join = trial.can_join()
            if not self.request.user.is_authenticated():
                can_join = False
            elif trial.invitation_set.filter(email=self.request.user.email).count() < 1:
                can_join = False
            context['can_join'] = can_join


        context['detail_template'] = detail_template
        context['page_title'] = page_title
        return context

class TrialQuestion(TrialByPkMixin, ContactView):
    """
    Asking a question to the owner of this trial.
    """

    def form_valid(self, form):
        """
        Praise be, someone has spammed us.
        """
        self.success_url = self.trial.get_absolute_url()
        form.send_email(to=self.trial.owner.email)
        return super(ContactView, self).form_valid(form)


class VariableInline(InlineFormSet):
    model = Variable
    form = VariableForm
    extra = 1


    def get_formset(self):
        """
        Returns the formset class from the inline formset factory
        """
        kwargs = self.get_factory_kwargs()
        kwargs['form'] = VariableForm
        return inlineformset_factory(self.model, self.get_inline_model(), **kwargs)

class TrialCreateLanding(TemplateView):
    """
    Redirect to the tutorial if anonymous
    """
    template_name = 'trials/new.html'
    def dispatch(self, *args, **kw):
        if not self.request.user.is_authenticated():
            return HttpResponseRedirect(reverse('tutorial'))
        return super(TrialCreateLanding, self).dispatch(*args, **kw)

class TrialCreate(LoginRequiredMixin, NamedFormsetsMixin, CreateWithInlinesView):
    model = Trial
    context_object_name = "trial"
    model               = Trial
    form_class          = TrialForm
    inlines = [VariableInline]
    inlines_names = ['Variable']

    def get_form(self, klass):
        """
        Add ownership details to the trial
        """
        form = super(TrialCreate, self).get_form(klass)
        form.instance.owner = self.request.user
        return form


class N1TrialCreate(LoginRequiredMixin, NamedFormsetsMixin, CreateWithInlinesView):
    model = Trial
    context_object_name = "trial"
    model               = Trial
    form_class          = N1TrialForm
    inlines = [VariableInline]
    inlines_names = ['Variable']

    def post(self, request, *args, **kwargs):
        """
        Handles POST requests, instantiating a form and formset instances with the passed
        POST variables and then checked for validity.
        """
        self.object = None
        form_class = self.get_form_class()
        form = self.get_form(form_class)

        if form.is_valid():
            self.object = form.save(commit=False)

            # TODO  - Clean this
            # TODO - Read the Django documentation
            # TODO - Convert this nonsense to the *actual* mixin hooks
            # FFS
            self.object.n1trial = True
            self.object.reporting_style = self.object.WHENEVER
            self.object.instruction_delivery = self.object.ON_DEMAND
            form_validated = True
        else:
            form_validated = False

        inlines = self.construct_inlines()

        if all_valid(inlines) and form_validated:
            response =  self.forms_valid(form, inlines)
            self.object.join(self.request.user)
            return response

        return self.forms_invalid(form, inlines)
        # Join up now.

    def get_form(self, klass):
        """
        Add ownership details to the trial
        """
        form = super(N1TrialCreate, self).get_form(klass)
        f2 = klass(data=self.request.POST)
        form.instance.owner = self.request.user
        return form

    def get_context_data(self, *args, **kw):
        """
        Add a flag to our context so that we know what type of trial we're
        creating.
        """
        context = super(N1TrialCreate, self).get_context_data(*args, **kw)
        context['n1trial'] = True
        return context

class ReproductionMixin(object):
    """
    Common functionality for reproducing any trial
    """
    @method_decorator(login_required)
    def dispatch(self, *args, **kw):
        self.parent = Trial.objects.get(pk=kw['pk'])
        self.reproducee = Trial.objects.reproduce(self.request.user, pk=kw['pk'])
        return super(ReproductionMixin, self).dispatch(*args, **kw)

    def construct_inlines(self):
        """
        Pass through a copy of the inlines for the previous trial's
        variable.
        """
        inline_formsets = super(ReproductionMixin, self).construct_inlines()
        old_var = self.parent.variable_set.all()[0]
        duplicated = old_var.duplicate()
        the_form = inline_formsets[0].forms[0]
        the_form.instance = duplicated
        the_form.fields['question'].initial = duplicated.question
        the_form.fields['style'].initial = duplicated.style
        return inline_formsets

    def get_form(self, klass):
        if self.request.method == 'GET':
            form = klass(instance=self.reproducee)
        else:
            form = super(ReproductionMixin, self).get_form(klass)
        form.instance.owner = self.request.user
        return form

    def get_context_data(self, *args, **kw):
        context = super(ReproductionMixin, self).get_context_data(*args, **kw)
        context['reproducing'] = True
        context['parent'] = self.parent
        context['reproducee'] = self.reproducee
        return context


class ReproduceTrial(ReproductionMixin, TrialCreate):
    """
    Reproduce a N>=1 trial for whatever reason.
    """


class ReproduceN1Trial(ReproductionMixin, N1TrialCreate):
    """
    Reproduce a N=1 trial in order to run it on ourself.
    """


class EditTrial(TrialByPkMixin, OwnsTrialMixin, UpdateView):
    """
    Edit a trial
    """
    form_class = TrialForm

    def get_object(self, *args, **kw):
        """
        We already have the trial from the mixin

        Return: Trial
        Exceptions: None
        """
        return self.trial

    def get_form(self, klass):
        """
        Add ownership details to the trial
        """
        form = super(EditTrial, self).get_form(klass)
        form.instance.owner = self.request.user
        form.instance.is_edited = True
        return form

    def get_context_data(self, *args, **kw):
        context = super(EditTrial, self).get_context_data(*args, **kw)
        context['editing'] = True
        return context



class PeekTrial(TrialByPkMixin, OwnsTrialMixin, TemplateView):
    """
    Peek at the results
    """
    template_name = 'trials/peek.html'


class InviteTrial(View):
    def post(self, *args, **kw):
        """
        Invite to the trial if:

        The user is the owner.
        The trial is invitation only.
        """
        trial = Trial.objects.get(pk=self.request.POST['trial_pk'])
        if self.request.user.is_authenticated() and self.request.user == trial.owner:
            if trial.recruitment == trial.INVITATION:
                invitation = Invitation.objects.get_or_create(trial=trial,
                                                              email=self.request.POST['email'])[0]
                invitation.invite()
                return HttpResponse('YAY')
        return HttpResponseForbidden('NO')


class StopTrial(TrialByPkMixin, OwnsTrialMixin, View):
    def post(self, *args, **kw):
        """
        Stop this trial.
        """
        self.trial.stop()
        return HttpResponseRedirect(self.trial.get_absolute_url())


class JoinTrial(LoginRequiredMixin, TemplateView):
    """
    Allow a user to join a trial
    """
    template_name = 'trials/join_trial.html'

    def __init__(self, *args, **kwargs):
        """
        Add an errors container
        """
        self.errors = []
        super(JoinTrial, self).__init__(*args, **kwargs)

    def get(self, *args, **kwargs):
        trial = Trial.objects.get(pk=kwargs['pk'])
        self.trial = trial
        return super(JoinTrial, self).get(self, *args, **kwargs)

    def post(self, *args, **kwargs):
        """
        Join the trial!
        """
        trial = Trial.objects.get(pk=kwargs['pk'])
        self.trial = trial
        user = self.request.user
        try:
            trial.join(user)
        except exceptions.TooManyParticipantsError:
            self.errors.append('Too many participants on this trial already')
        except exceptions.AlreadyJoinedError:
            self.errors.append('You were already participating in this trial!')
        except exceptions.TrialOwnerError:
            self.errors.append('This is your trial - joining it would be wonky!')
        except exceptions.TrialFinishedError:
            self.errors.append('This trial has already finished!')
        return super(JoinTrial, self).get(self, * args, **kwargs)

    def get_context_data(self, **kw):
        """
        We'd like access to the trial in our joined template
        """
        context = super(JoinTrial, self).get_context_data(**kw)
        context['errors'] = self.errors
        context['trial']  = self.trial
        return context


class LeaveTrial(LoginRequiredMixin, TemplateView):
    """
    Allow a user to leave a trial
    """
    template_name = 'trials/leave_trial.html'

    def __init__(self, *args, **kwargs):
        """
        Add an errors container
        """
        self.errors = []
        super(LeaveTrial, self).__init__(*args, **kwargs)

    def get(self, *args, **kwargs):
        trial = Trial.objects.get(pk=kwargs['pk'])
        self.trial = trial
        return super(LeaveTrial, self).get(self, *args, **kwargs)

    def post(self, *args, **kwargs):
        """
        Join the trial!
        """
        trial = Trial.objects.get(pk=kwargs['pk'])
        self.trial = trial
        user = self.request.user
        participant = trial.participant_set.get(user=user)
        participant.user = None
        participant.save()
        return super(LeaveTrial, self).get(self, * args, **kwargs)

    def get_context_data(self, **kw):
        """
        We'd like access to the trial in our joined template
        """
        context = super(LeaveTrial, self).get_context_data(**kw)
        context['errors'] = self.errors
        context['trial']  = self.trial
        return context


class TrialAsCsv(View):
    """
    Download the trial's raw data as a csv.
    """

    @serve_maybe
    def get(self, request, pk):
        """
        We want to serve a CSV of this trial's raw data!

        Return: str
        Exceptions: None
        """
        trial = Trial.objects.get(pk=pk)
        rows = [
            (report.group.name, report.date.isoformat(), report.score)
            for report in trial.report_set.all()
            ]

        raw = ffs.Path.newfile()
        with raw.csv() as csv:
            csv.writerows([[
                    'group',
                    'date'
                    'score'
                    ]] + rows)
        return raw


# Views for trial discovery - lists, featured, etc.

class AllTrials(TemplateView):
    """
    The all trials tab of the site
    """
    template_name = 'trials.html'

    def get_context_data(self, **kw):
        """
        Add popular and featured trials to the all trials page
        """
        context = super(AllTrials, self).get_context_data(**kw)
        today = datetime.datetime.today()
        active = Trial.objects.filter(recruitment=Trial.ANYONE, private=False, n1trial=False)

        if self.request.user.is_authenticated():
            active = active.exclude(owner=self.request.user).exclude(participant__user=self.request.user)
        context['active'] = active
        # context['past'] = Trial.objects.completed()
        return context


class FeaturedTrialsList(ListView):
    """
    This is the list view for featured Trials - an editorially
    decided subset of all trials.
    """
    queryset            = Trial.objects.filter(featured=True, private=False)
    context_object_name = 'trials'
    template_name       = 'trials/featured_trial_list.html'


class TrialSearchView(ListView):
    """
    Called from the search bar in the top right corner.
    """
    context_object_name = 'trials'
    template_name = 'trials/search_results_list.html'

    def get_queryset(self):
        """
        Return our queryset please.

        Return: Queryset
        Exceptions: None
        """
        q = self.request.GET.get('q', '')
        if not q:
            return Trial.objects.all()
        return Trial.objects.filter(title__icontains=q)


class RandomiseMeView(TrialByPkMixin, LoginRequiredMixin, View):
    """
    A user has requested the latest set of instructions in
    a N=1 trial. Randomise them and initialise the report.
    """
    def post(self, *args, **kw):
        """
        Create a report, randomise the user and tell them their
        latest group!

        Return: HttpResponse
        Exceptions: None
        """
        group = random.choice(self.trial.ensure_groups())
        participant = self.trial.participant_set.get(user=self.request.user)
        report = Report.objects.get_or_create(
            trial=self.trial,
            participant=participant,
            date__isnull=True,
            variable=self.trial.variable_set.all()[0])[0]
        report.group = group
        report.save()

        return HttpResponse(group.name.lower())


class TutorialView(FormView):
    """
    Ultra-simple process for creating a N=1 trial for people
    """
    model = Trial
    form_class = TutorialForm
    template_name='trials/tutorial.html'

    def get_form_kwargs(self, *args, **kw):
        """
        Add auto_id=False
        """
        kwargs = super(TutorialView, self).get_form_kwargs(*args, **kw)
        kwargs['auto_id'] = False
        kwargs['request'] = self.request
        return kwargs

    def _login_or_signup(self, form):
        """
        Given the valid FORM, return a user, signing them up if required,
        and logging them in

        Return: RMUser
        Exceptions: None
        """
        email, pw, pw2 = [form.data.get(x) for x in ['email', 'password', 'password_confirmation']]
        if form.user is None:
            # We need to sign 'em up!
            sign_me_up(self.request, email, pw, pw2)

        user = authenticate(username=email, password=pw)
        login(self.request, user)
        return user

    def form_valid(self, form):
        """
        Create the user if required then create the trial.

        If the user already exists we can just log them in as password
        verification for email addresses we already know about is handled by the form.
        """
        if self.request.user.is_authenticated():
            user = self.request.user
        else:
            user = self._login_or_signup(form)

        title = form.data['title']
        group_a, group_b = form.data['group_a'], form.data['group_b']
        measure_style, measure_question = [form.data[v]
                                           for v in ['measure_style',
                                                     'measure_question']]

        trial = n1_with_sane_defaults(user, title, group_a, group_b,
                                      measure_style, measure_question)
        return HttpResponseRedirect(trial.get_absolute_url())


class TutorialFromExampleView(TutorialView):
    """
    Let the user take the tutorial, but this is a worked example, with
    defaults pre-filled
    """
    def get_initial(self):
        tutorial = TutorialExample.objects.get(**self.kwargs)
        print tutorial
        initial = dict(
            title=tutorial.question,
            measure_style=tutorial.measure_style,
            measure_question=tutorial.measure_question,
            group_a=tutorial.group_a,
            group_b=tutorial.group_b
            )
        print initial
        return initial
